"""Общие помощники хендлеров: проверка доступа и отправка ответов."""

from __future__ import annotations

import logging
from functools import wraps
from typing import Any

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from ..access import describe_user
from ..app import AppContext, chat_key_for, get_app, thread_id_for
from ..storage import STATUS_BLOCKED, STATUS_PENDING
from ..ui import access_menu

log = logging.getLogger(__name__)


async def reply(update: Update, text: str, **kwargs: Any):
    """Ответить в тот же чат/тред, с HTML-разметкой по умолчанию."""
    message = update.effective_message
    if message is None:
        return None
    kwargs.setdefault("parse_mode", ParseMode.HTML)
    return await message.reply_text(text, **kwargs)


async def send(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str, **kwargs: Any):
    kwargs.setdefault("parse_mode", ParseMode.HTML)
    return await context.bot.send_message(chat_id=chat_id, text=text, **kwargs)


def guarded(func):
    """Пускать дальше только владельца и одобренных пользователей."""

    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        app = get_app(context)
        user = update.effective_user
        if user is None or user.is_bot:
            return None

        decision = await app.access.check(user)
        if decision.allowed:
            return await func(update, context)

        if decision.status == STATUS_BLOCKED:
            return None

        if decision.just_requested:
            await _notify_owner(app, context, update)
            await _answer(
                update,
                "🔒 Этот бот приватный. Я отправила запрос владельцу — "
                "как только доступ откроют, просто напиши снова.",
            )
        elif decision.status == STATUS_PENDING:
            await _answer(update, "🕓 Заявка уже отправлена владельцу, жду решения.")
        return None

    return wrapper


def owner_only(func):
    """Команды управления — только для владельца."""

    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        app = get_app(context)
        user = update.effective_user
        if user is None or not app.access.is_owner(user.id):
            await _answer(update, "⛔️ Команда только для владельца бота.")
            return None
        return await func(update, context)

    return wrapper


async def _answer(update: Update, text: str) -> None:
    if update.callback_query is not None:
        await update.callback_query.answer(text[:200], show_alert=True)
        return
    await reply(update, text)


async def _notify_owner(app: AppContext, context: ContextTypes.DEFAULT_TYPE, update: Update) -> None:
    user = update.effective_user
    if user is None:
        return
    text = (
        "🔔 <b>Запрос доступа</b>\n"
        f"{describe_user(user)}\n\n"
        "Открыть доступ к боту?"
    )
    try:
        await context.bot.send_message(
            chat_id=app.settings.owner_id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=access_menu(user.id),
        )
    except Exception:  # noqa: BLE001 — владелец мог не начать диалог с ботом
        log.warning("Не смог уведомить владельца о запросе доступа", exc_info=True)


def context_of(update: Update) -> tuple[str, int | None]:
    """(chat_key, thread_id) — то, чем адресуется сессия."""
    return chat_key_for(update), thread_id_for(update)
