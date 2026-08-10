"""Отчёт об обновлении: он показывается в Telegram, значит должен быть валидным HTML."""

from claude_tg.updater import Step, UpdateReport


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


def test_missing_versions_render_as_question_mark():
    text = UpdateReport().to_text()
    assert "<code>?</code>" in text


def test_steps_marked_by_outcome():
    report = UpdateReport(steps=[Step("ок", True), Step("не ок", False, "детали")])
    text = report.to_text()
    assert "✅ ок" in text
    assert "⚠️ не ок — детали" in text
