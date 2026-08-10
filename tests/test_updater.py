"""Обновление компонентов: отчёт, прогресс и обход установки через Homebrew."""

import pytest

from claude_tg import updater
from claude_tg.updater import Step, UpdateReport


@pytest.fixture
def fake_commands(monkeypatch):
    """Подменяем запуск процессов: собираем команды, ничего не выполняя."""
    calls: list[list[str]] = []

    async def fake_run(cmd, timeout=updater.TIMEOUT, on_line=None):
        calls.append(list(cmd))
        if on_line is not None:
            await on_line("строка вывода")
        return 0, "готово"

    monkeypatch.setattr(updater, "_run", fake_run)
    monkeypatch.setattr(updater, "sdk_version", lambda: "0.2.134")
    return calls


def with_cask(monkeypatch, name: str | None, version: str = "2.1.205"):
    async def cli_version(binary=None):
        return version

    async def brew_cask_name():
        return name

    monkeypatch.setattr(updater, "cli_version", cli_version)
    monkeypatch.setattr(updater, "brew_cask_name", brew_cask_name)


def _collect(sink: list[updater.ProgressEvent]):
    async def progress(event: updater.ProgressEvent) -> None:
        sink.append(event)

    return progress


async def test_brew_is_the_update_path_for_cask_installs(fake_commands, monkeypatch):
    """Каск управляет версией сам — `claude update` его не сдвинет."""
    with_cask(monkeypatch, "claude-code")

    report = await updater.run_update("claude", with_sdk=False)

    assert ["brew", "upgrade", "--cask", "claude-code"] in fake_commands
    assert any("brew upgrade" in step.name for step in report.steps)
    assert report.notes, "про канал stable нужно предупредить"
    assert "Версии не изменились" in report.to_text()


async def test_latest_cask_gets_no_stable_warning(fake_commands, monkeypatch):
    with_cask(monkeypatch, "claude-code@latest")

    report = await updater.run_update("claude", with_sdk=False)

    assert ["brew", "upgrade", "--cask", "claude-code@latest"] in fake_commands
    assert not report.notes


async def test_without_brew_no_brew_step(fake_commands, monkeypatch):
    with_cask(monkeypatch, None)

    report = await updater.run_update("claude", with_sdk=False)

    assert not any("brew" in step.name for step in report.steps)


async def test_progress_plan_is_counted_upfront(fake_commands, monkeypatch):
    with_cask(monkeypatch, "claude-code")
    seen: list[updater.ProgressEvent] = []

    await updater.run_update("claude", progress=_collect(seen))

    # claude update + brew upgrade + pip install
    assert {e.total for e in seen} == {3}
    labels = [e.label for e in seen]
    assert labels[0] == "claude update"
    assert any(label.startswith("brew upgrade") for label in labels)
    assert any(label.startswith("pip install") for label in labels)

    assert seen[-1].label == "готово"
    assert seen[-1].fraction == 1.0


async def test_progress_carries_command_output(fake_commands, monkeypatch):
    with_cask(monkeypatch, None)
    seen: list[updater.ProgressEvent] = []

    await updater.run_update("claude", with_sdk=False, progress=_collect(seen))

    assert any(e.line == "строка вывода" for e in seen), "живой вывод должен доходить"


async def test_fraction_never_leaves_bounds():
    assert updater.ProgressEvent(1, 3, "a").fraction == 0.0
    assert updater.ProgressEvent(2, 3, "a").fraction == pytest.approx(1 / 3)
    assert updater.ProgressEvent(9, 3, "a").fraction == 1.0
    assert updater.ProgressEvent(1, 0, "a").fraction == 0.0


async def test_broken_progress_does_not_break_update(fake_commands, monkeypatch):
    with_cask(monkeypatch, None)

    async def exploding(event: updater.ProgressEvent) -> None:
        raise RuntimeError("сообщение удалили")

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
    assert "/restart" in bumped.to_text()

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
