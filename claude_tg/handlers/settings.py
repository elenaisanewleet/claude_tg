"""Настройки: модель, effort, режим доступа, thinking, что показывать."""

from __future__ import annotations

import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from .. import catalog, ui
from ..app import get_app
from ..storage import Prefs
from .common import context_of, guarded, reply

log = logging.getLogger(__name__)

# Ключ каталога → поле в Prefs
PREF_FIELD = {
    "model": "model",
    "effort": "effort",
    "mode": "permission_mode",
    "thinking": "thinking",
}


def settings_text(prefs: Prefs) -> str:
    return (
        "⚙️ <b>Настройки сессии</b>\n\n"
        f"🧠 Модель: <code>{prefs.model}</code>\n"
        f"⚡ Effort: <code>{prefs.effort}</code>\n"
        f"🎛 Режим доступа: {catalog.label('mode', prefs.permission_mode)}\n"
        f"💭 Thinking: {catalog.label('thinking', prefs.thinking)}\n\n"
        "Меняются на лету: модель и режим доступа. "
        "Effort и thinking перезапускают процесс — контекст сессии сохраняется."
    )


@guarded
async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = get_app(context)
    chat_key, _ = context_of(update)
    prefs = await app.prefs_for(chat_key)
    await reply(update, settings_text(prefs), reply_markup=ui.main_menu(prefs))


async def _set_and_report(
    update: Update, context: ContextTypes.DEFAULT_TYPE, kind: str, value: str
) -> bool:
    app = get_app(context)
    chat_key, _ = context_of(update)
    if not catalog.is_valid(kind, value):
        await reply(update, f"Не знаю такого значения: <code>{value}</code>")
        return False

    prefs = await app.prefs_for(chat_key)
    setattr(prefs, PREF_FIELD[kind], value)
    await app.save_prefs(chat_key, prefs)
    return True


@guarded
async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = get_app(context)
    chat_key, _ = context_of(update)
    prefs = await app.prefs_for(chat_key)
    if context.args:
        value = context.args[0].strip()
        if await _set_and_report(update, context, "model", value):
            await reply(update, f"🧠 Модель: <code>{value}</code>")
        return
    await reply(
        update,
        f"Сейчас: <code>{prefs.model}</code>\nАлиасы всегда указывают на свежую версию.",
        reply_markup=ui.model_menu(prefs),
    )


@guarded
async def cmd_effort(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = get_app(context)
    chat_key, _ = context_of(update)
    prefs = await app.prefs_for(chat_key)
    if context.args:
        value = context.args[0].strip().lower()
        if await _set_and_report(update, context, "effort", value):
            await reply(update, f"⚡ Effort: <code>{value}</code>")
        return
    hints = "\n".join(f"{c.label} — {c.hint}" for c in catalog.EFFORTS)
    await reply(
        update,
        f"Сейчас: <code>{prefs.effort}</code>\n\n{hints}",
        reply_markup=ui.choice_menu("effort", catalog.EFFORTS, prefs.effort),
    )


@guarded
async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = get_app(context)
    chat_key, _ = context_of(update)
    prefs = await app.prefs_for(chat_key)
    if context.args:
        value = context.args[0].strip()
        if await _set_and_report(update, context, "mode", value):
            await reply(update, f"🎛 Режим: {catalog.label('mode', value)}")
        return
    hints = "\n".join(f"{c.label} — {c.hint}" for c in catalog.PERMISSION_MODES)
    await reply(
        update,
        f"Сейчас: {catalog.label('mode', prefs.permission_mode)}\n\n{hints}",
        reply_markup=ui.choice_menu("mode", catalog.PERMISSION_MODES, prefs.permission_mode),
    )


@guarded
async def cmd_thinking(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = get_app(context)
    chat_key, _ = context_of(update)
    prefs = await app.prefs_for(chat_key)
    if context.args:
        value = context.args[0].strip()
        if await _set_and_report(update, context, "thinking", value):
            await reply(update, f"💭 Thinking: {catalog.label('thinking', value)}")
        return
    hints = "\n".join(f"{c.label} — {c.hint}" for c in catalog.THINKING_MODES)
    await reply(
        update,
        f"Сейчас: {catalog.label('thinking', prefs.thinking)}\n\n{hints}",
        reply_markup=ui.choice_menu("thinking", catalog.THINKING_MODES, prefs.thinking),
    )


@guarded
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка menu:*, set:*, tgl:* — навигация и переключатели."""
    query = update.callback_query
    if query is None or not query.data:
        return
    app = get_app(context)
    chat_key, _ = context_of(update)
    data = query.data

    if data == "menu:close":
        await query.answer()
        try:
            await query.message.delete()
        except Exception:  # noqa: BLE001 — сообщение могли уже удалить
            pass
        return

    if data == "menu:noop":
        await query.answer()
        return

    prefs = await app.prefs_for(chat_key)

    if data.startswith("set:"):
        _, kind, value = data.split(":", 2)
        if not catalog.is_valid(kind, value):
            await query.answer("Неизвестное значение", show_alert=True)
            return
        setattr(prefs, PREF_FIELD[kind], value)
        await app.save_prefs(chat_key, prefs)
        await query.answer(f"Готово: {value}")
        await _render(query, "main", prefs)
        return

    if data.startswith("tgl:"):
        flag = data.split(":", 1)[1]
        if not hasattr(prefs, flag):
            await query.answer("Неизвестный переключатель", show_alert=True)
            return
        setattr(prefs, flag, not getattr(prefs, flag))
        await app.save_prefs(chat_key, prefs)
        await query.answer()
        await _render(query, "view", prefs)
        return

    if data.startswith("menu:"):
        screen = data.split(":", 1)[1]
        await query.answer()
        if screen == "status":
            from .sessions import status_text

            session = app.sessions.peek(chat_key)
            await query.edit_message_text(
                await status_text(app, chat_key, session),
                parse_mode=ParseMode.HTML,
                reply_markup=ui.main_menu(prefs),
            )
            return
        await _render(query, screen, prefs)


async def _render(query, screen: str, prefs: Prefs) -> None:
    if screen == "model":
        markup = ui.model_menu(prefs)
        text = f"🧠 Модель: <code>{prefs.model}</code>"
    elif screen == "effort":
        markup = ui.choice_menu("effort", catalog.EFFORTS, prefs.effort)
        text = "⚡ Глубина рассуждений:\n" + "\n".join(
            f"{c.label} — {c.hint}" for c in catalog.EFFORTS
        )
    elif screen == "mode":
        markup = ui.choice_menu("mode", catalog.PERMISSION_MODES, prefs.permission_mode)
        text = "🎛 Режим доступа к инструментам:\n" + "\n".join(
            f"{c.label} — {c.hint}" for c in catalog.PERMISSION_MODES
        )
    elif screen == "thinking":
        markup = ui.choice_menu("thinking", catalog.THINKING_MODES, prefs.thinking)
        text = "💭 Размышления:\n" + "\n".join(
            f"{c.label} — {c.hint}" for c in catalog.THINKING_MODES
        )
    elif screen == "view":
        markup = ui.view_menu(prefs)
        text = "👁 Что показывать в ответах"
    else:
        markup = ui.main_menu(prefs)
        text = settings_text(prefs)

    try:
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
    except Exception:  # noqa: BLE001 — «message is not modified» и подобное
        log.debug("Не смог перерисовать меню", exc_info=True)
