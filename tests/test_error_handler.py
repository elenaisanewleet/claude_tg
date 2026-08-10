"""Обработчик ошибок: шумные сетевые сбои не должны выглядеть как паника."""

import logging
from dataclasses import dataclass
from typing import Any

from telegram.error import Conflict, NetworkError

from claude_tg.bot import _on_error


@dataclass
class FakeContext:
    error: Any


async def test_conflict_logs_one_clear_line(caplog):
    caplog.set_level(logging.ERROR, logger="claude_tg.bot")
    await _on_error(None, FakeContext(Conflict("terminated by other getUpdates request")))

    records = [r for r in caplog.records if r.name == "claude_tg.bot"]
    assert len(records) == 1
    assert records[0].exc_info is None, "трассировка тут только мешает"
    assert "экземпляр" in records[0].message


async def test_network_error_is_a_warning(caplog):
    caplog.set_level(logging.WARNING, logger="claude_tg.bot")
    await _on_error(None, FakeContext(NetworkError("connection dropped")))

    records = [r for r in caplog.records if r.name == "claude_tg.bot"]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert records[0].exc_info is None


async def test_unknown_error_keeps_traceback(caplog):
    caplog.set_level(logging.ERROR, logger="claude_tg.bot")
    await _on_error(None, FakeContext(RuntimeError("что-то новое")))

    records = [r for r in caplog.records if r.name == "claude_tg.bot"]
    assert len(records) == 1
    assert records[0].exc_info is not None, "незнакомые ошибки разбираем по трассировке"
