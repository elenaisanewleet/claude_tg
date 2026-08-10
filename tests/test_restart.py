"""Перезапуск по команде: процесс должен останавливаться, а не висеть."""

import pytest

from claude_tg.handlers import admin


class FakeApplication:
    def __init__(self) -> None:
        self.stopped = False

    def stop_running(self) -> None:
        self.stopped = True


@pytest.fixture(autouse=True)
def no_delay(monkeypatch):
    monkeypatch.setattr(admin, "RESTART_DELAY", 0)


async def test_stop_soon_stops_the_application():
    application = FakeApplication()
    await admin._stop_soon(application)
    assert application.stopped is True


async def test_report_points_at_the_restart_command():
    """Обновление бесполезно, если о том, как его подхватить, знает только терминал."""
    from claude_tg.updater import UpdateReport

    text = UpdateReport(cli_before="2.1.205", cli_after="2.1.220").to_text()
    assert "/restart" in text
