"""Сборка Telegram-приложения: хендлеры, расписания, жизненный цикл."""

from __future__ import annotations

import datetime as dt
import logging

from telegram import BotCommand, Update
from telegram.constants import ParseMode
from telegram.error import Conflict, NetworkError
from telegram.ext import (
    AIORateLimiter,
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import updater
from .access import AccessControl
from .app import BOT_DATA_KEY, AppContext
from .bridge import SessionManager
from .config import Settings
from .handlers import admin, chat, permissions, sessions
from .handlers import settings as settings_handlers
from .storage import Storage

log = logging.getLogger(__name__)

COMMANDS = [
    BotCommand("new", "Новая сессия"),
    BotCommand("sessions", "Список сессий"),
    BotCommand("stop", "Прервать текущий ход"),
    BotCommand("status", "Статус и расход контекста"),
    BotCommand("settings", "Настройки"),
    BotCommand("model", "Выбрать модель"),
    BotCommand("effort", "Глубина рассуждений"),
    BotCommand("mode", "Режим доступа к инструментам"),
    BotCommand("thinking", "Режим размышлений"),
    BotCommand("ws", "Рабочая папка"),
    BotCommand("version", "Версии Claude Code и бота"),
    BotCommand("id", "Показать user_id"),
    BotCommand("help", "Справка"),
]


def build_application(settings: Settings) -> Application:
    settings.ensure_dirs()

    application = (
        ApplicationBuilder()
        .token(settings.bot_token)
        .rate_limiter(AIORateLimiter())
        .concurrent_updates(True)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    application.bot_data["settings"] = settings
    _register(application)
    return application


def _register(application: Application) -> None:
    application.add_handler(CommandHandler(["start"], admin.cmd_start))
    application.add_handler(CommandHandler(["help"], admin.cmd_help))
    application.add_handler(CommandHandler(["id"], admin.cmd_id))
    application.add_handler(CommandHandler(["version"], admin.cmd_version))
    application.add_handler(CommandHandler(["update"], admin.cmd_update))
    application.add_handler(CommandHandler(["users"], admin.cmd_users))
    application.add_handler(CommandHandler(["grant"], admin.cmd_grant))
    application.add_handler(CommandHandler(["revoke"], admin.cmd_revoke))
    application.add_handler(CommandHandler(["block"], admin.cmd_block))

    application.add_handler(CommandHandler(["settings"], settings_handlers.cmd_settings))
    application.add_handler(CommandHandler(["model"], settings_handlers.cmd_model))
    application.add_handler(CommandHandler(["effort"], settings_handlers.cmd_effort))
    application.add_handler(CommandHandler(["mode"], settings_handlers.cmd_mode))
    application.add_handler(CommandHandler(["thinking"], settings_handlers.cmd_thinking))

    application.add_handler(CommandHandler(["new"], sessions.cmd_new))
    application.add_handler(CommandHandler(["sessions"], sessions.cmd_sessions))
    application.add_handler(CommandHandler(["resume"], sessions.cmd_resume))
    application.add_handler(CommandHandler(["fork"], sessions.cmd_fork))
    application.add_handler(CommandHandler(["status"], sessions.cmd_status))
    application.add_handler(CommandHandler(["ws", "workspace"], sessions.cmd_workspace))
    application.add_handler(CommandHandler(["stop"], chat.on_stop))

    application.add_handler(CallbackQueryHandler(permissions.on_permission_callback, pattern=r"^perm:"))
    application.add_handler(CallbackQueryHandler(admin.on_access_callback, pattern=r"^acc:"))
    application.add_handler(CallbackQueryHandler(sessions.on_session_callback, pattern=r"^(sess|sres):"))
    application.add_handler(
        CallbackQueryHandler(settings_handlers.on_callback, pattern=r"^(menu|set|tgl):")
    )

    media = (
        filters.Document.ALL
        | filters.PHOTO
        | filters.VOICE
        | filters.AUDIO
        | filters.VIDEO
        | filters.VIDEO_NOTE
    )
    incoming = ((filters.TEXT & ~filters.COMMAND) | media) & ~filters.StatusUpdate.ALL
    application.add_handler(MessageHandler(incoming, chat.on_message))
    application.add_error_handler(_on_error)


async def _post_init(application: Application) -> None:
    settings: Settings = application.bot_data["settings"]

    storage = Storage(settings.db_path)
    await storage.connect()
    manager = SessionManager(
        settings.workspace_root,
        cli_path=settings.claude_cli,
        turn_timeout=settings.turn_timeout,
    )
    app = AppContext(
        settings=settings,
        storage=storage,
        access=AccessControl(settings, storage),
        sessions=manager,
        bot=application.bot,
    )
    application.bot_data[BOT_DATA_KEY] = app

    try:
        await application.bot.set_my_commands(COMMANDS)
    except Exception:  # noqa: BLE001 — не критично
        log.warning("Не смог зарегистрировать команды", exc_info=True)

    if settings.auto_update and application.job_queue is not None:
        application.job_queue.run_daily(
            _auto_update_job,
            time=dt.time(hour=settings.auto_update_hour, minute=17),
            name="claude-tg-autoupdate",
        )
        log.info("Автообновление включено: каждый день в %02d:17", settings.auto_update_hour)

    cli = await updater.cli_version(settings.claude_cli)
    log.info(
        "claude-tg запущен · Claude Code %s · SDK %s",
        cli or "не найден",
        updater.sdk_version() or "?",
    )
    if not cli:
        log.warning(
            "CLI `claude` не найден. Установи Claude Code или задай CLAUDE_CLI_PATH — "
            "без него бот не сможет отвечать."
        )


async def _post_shutdown(application: Application) -> None:
    app: AppContext | None = application.bot_data.get(BOT_DATA_KEY)
    if app is None:
        return
    await app.sessions.close_all()
    await app.storage.close()


async def _auto_update_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    app: AppContext = context.application.bot_data[BOT_DATA_KEY]
    report = await updater.run_update(app.settings.claude_cli)
    if not report.changed:
        log.info("Автообновление: всё уже актуально (%s)", report.cli_after)
        return
    log.info("Автообновление: %s → %s", report.cli_before, report.cli_after)
    try:
        await context.bot.send_message(
            chat_id=app.settings.owner_id,
            text=report.to_text(),
            parse_mode=ParseMode.HTML,
        )
    except Exception:  # noqa: BLE001
        log.debug("Не смог отправить отчёт об автообновлении", exc_info=True)


async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    error = context.error

    # Один и тот же токен слушают два процесса. Telegram оставляет только
    # последний, а остальные получают Conflict на каждом опросе — трассировка
    # тут ничего не объясняет, нужен понятный совет.
    if isinstance(error, Conflict):
        log.error(
            "Этот же токен слушает другой запущенный экземпляр бота — Telegram "
            "разрешает только один. Проверь: ./deploy/autostart-macos.sh --status "
            "и pgrep -fl claude_tg, оставь что-то одно."
        )
        return

    # Пропал интернет или моргнул Telegram: библиотека сама повторит запрос.
    if isinstance(error, NetworkError):
        log.warning("Сеть недоступна, повторю: %s", error)
        return

    log.exception("Необработанная ошибка при обработке апдейта", exc_info=error)
    if not isinstance(update, Update):
        return
    message = update.effective_message
    if message is None:
        return
    try:
        await message.reply_text("⚠️ Что-то пошло не так. Я записала ошибку в лог.")
    except Exception:  # noqa: BLE001
        pass
