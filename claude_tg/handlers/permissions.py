"""Ответы на запросы разрешений инструментов (кнопки под «Разрешить X?»)."""

from __future__ import annotations

import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from ..app import get_app
from .common import guarded

log = logging.getLogger(__name__)

_LABELS = {
    "allow": "✅ Разрешено",
    "always": "♾ Разрешено на всю сессию",
    "deny": "⛔️ Запрещено",
}


@guarded
async def on_permission_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or not query.data:
        return

    app = get_app(context)
    _, request_id, decision = query.data.split(":", 2)
    pending = app.resolve_permission(request_id, decision)
    if pending is None:
        await query.answer("Запрос уже неактуален", show_alert=True)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:  # noqa: BLE001
            pass
        return

    await query.answer(_LABELS.get(decision, "Готово"))
    try:
        await query.edit_message_text(
            f"{_LABELS.get(decision, decision)}: <code>{pending.tool_name}</code>",
            parse_mode=ParseMode.HTML,
        )
    except Exception:  # noqa: BLE001
        log.debug("Не смог обновить сообщение разрешения", exc_info=True)
