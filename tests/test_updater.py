"""Обновление компонентов: отчёт, прогресс и обход установки через Homebrew."""

import pytest

from claude_tg import updater
from claude_tg.updater import Step, UpdateReport


@pytest.fixture
def fake_commands(monkeypatch):
    """Подменяем запуск процессов: собираем команды, ничего не выполняя."""
    calls: list[list[str]] = []

    async def fake_run(cmd, timeout=updater.TIMEOUT):
        calls.append(list(cmd))
        return 0, "готово"

    monkeypatch.setattr(updater, "_run", fake_run)
    monkeypatch.setattr(updater, "sdk_version", lambda: "0.2.134")
    return calls


async def test_brew_upgrade_when_version_did_not_move(fake_commands, monkeypatch):
    """Claude Code из Homebrew: `claude update` отрабатывает, но версия та же."""

    async def stuck_version(binary=None):
        return "2.1.205"

    async def cask():
        return "claude-code"

    monkeypatch.setattr(updater, "cli_version", stuck_version)
    monkeypatch.setattr(updater, "brew_cask_name", cask)

    report = await updater.run_update("claude", with_sdk=False)

    assert ["brew", "upgrade", "--cask", "claude-code"] in fake_commands
    assert any("brew upgrade" in step.name for step in report.steps)
    assert report.notes, "про канал stable нужно предупредить"
    assert "Версии не изменились" in report.to_text()


async def test_no_brew_step_when_version_moved(fake_commands, monkeypatch):
    versions = iter(["2.1.205", "2.1.226", "2.1.226"])

    async def moving_version(binary=None):
        return next(versions)

    async def cask():  # pragma: no cover — не должен вызываться
        raise AssertionError("brew трогать не нужно, версия уже обновилась")

    monkeypatch.setattr(updater, "cli_version", moving_version)
    monkeypatch.setattr(updater, "brew_cask_name", cask)

    report = await updater.run_update("claude", with_sdk=False)

    assert not any("brew" in step.name for step in report.steps)
    assert report.changed is True


async def test_progress_reports_each_step(fake_commands, monkeypatch):
    seen: list[str] = []

    async def version(binary=None):
        return "2.1.205"

    async def cask():
        return None

    monkeypatch.setattr(updater, "cli_version", version)
    monkeypatch.setattr(updater, "brew_cask_name", cask)

    await updater.run_update("claude", progress=_collect(seen))

    assert seen[0] == "claude update"
    assert any("pip install" in step for step in seen)


def _collect(sink: list[str]):
    async def progress(step: str) -> None:
        sink.append(step)

    return progress


async def test_broken_progress_does_not_break_update(fake_commands, monkeypatch):
    async def version(binary=None):
        return "2.1.205"

    async def cask():
        return None

    async def exploding(step: str) -> None:
        raise RuntimeError("сообщение удалили")

    monkeypatch.setattr(updater, "cli_version", version)
    monkeypatch.setattr(updater, "brew_cask_name", cask)

    report = await updater.run_update("claude", progress=exploding)
    assert report.steps, "обновление должно доработать до конца"


def test_command_output_is_escaped():
    """Вывод CLI попадает в сообщение — без экранирования Telegram его отвергнет."""
    report = UpdateReport(
        cli_before="2.1.205 <old>",
        cli_after="2.1.226 & new",
        sdk_before="0.2.1",
        sdk_after="0.2.2",
        steps=[Step("claude update", False, "error: unexpected <token> & retry")],
    )
    text = report.to_text()

    assert "<old>" not in text
    assert "&lt;old&gt;" in text
    assert "&amp; new" in text
    assert "&lt;token&gt;" in text
    # Наши собственные теги остаются на месте.
    assert "<b>Обновление компонентов</b>" in text
    assert "<code>" in text


def test_changed_detects_version_bump():
    same = UpdateReport(cli_before="2.1.205", cli_after="2.1.205", sdk_before="1", sdk_after="1")
    assert same.changed is False

    bumped = UpdateReport(cli_before="2.1.205", cli_after="2.1.226", sdk_before="1", sdk_after="1")
    assert bumped.changed is True
    assert "перезапусти" in bumped.to_text()

    sdk_only = UpdateReport(cli_before="2.1.205", cli_after="2.1.205", sdk_before="1", sdk_after="2")
    assert sdk_only.changed is True


def test_unchanged_versions_say_so_plainly():
    text = UpdateReport(cli_before="2.1.205", cli_after="2.1.205").to_text()
    assert "Версии не изменились" in text
    assert "перезапусти" not in text


def test_notes_are_rendered_and_escaped():
    report = UpdateReport(notes=["каск claude-code & stable <канал>"])
    text = report.to_text()
    assert "ℹ️" in text
    assert "&amp; stable &lt;канал&gt;" in text


def test_missing_versions_render_as_question_mark():
    text = UpdateReport().to_text()
    assert "<code>?</code>" in text


def test_steps_marked_by_outcome():
    report = UpdateReport(steps=[Step("ок", True), Step("не ок", False, "детали")])
    text = report.to_text()
    assert "✅ ок" in text
    assert "⚠️ не ок — детали" in text
