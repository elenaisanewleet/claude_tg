"""Точка входа: `python -m claude_tg` или консольная команда `claude-tg`."""

from __future__ import annotations

import logging
import sys

from telegram import Update

from .bot import build_application
from .config import ConfigError, Settings


def setup_logging(level: str) -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
        level=getattr(logging, level, logging.INFO),
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram.ext.Application").setLevel(logging.INFO)


def main() -> int:
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        print("Скопируй .env.example в .env и заполни поля.", file=sys.stderr)
        return 2

    setup_logging(settings.log_level)
    application = build_application(settings)
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
