"""Команды владельца и базовые: /start, /help, /id, доступы, обновления."""

from __future__ import annotations

import html
import logging
import re

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from .. import updater
from ..access import describe_user
from ..app import get_app
from .common import guarded, owner_only, reply

log = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")

HELP = """🤖 <b>Claude в Telegram</b>

Просто пиши — я передам сообщение Claude Code и покажу ответ по мере генерации.
Файлы, фото и документы можно кидать прямо в чат: они попадают в рабочую папку.

<b>Диалог</b>
/new — начать новую сессию
/sessions — список сессий, вернуться к любой
/resume &lt;id&gt; — продолжить конкретную сессию
/fork — ответвить копию текущей
/stop — прервать текущий ход
/status — модель, режим, расход контекста
/ws — рабочая папка и её содержимое

<b>Настройки</b>
/settings — общее меню
/model [алиас|имя] — модель (<code>opus</code>, <code>sonnet</code>, <code>fable</code>, <code>haiku</code>)
/effort [low|medium|high|xhigh|max] — глубина рассуждений
/mode [default|acceptEdits|plan|dontAsk|auto|bypassPermissions] — режим доступа
/thinking [adaptive|summarized|disabled] — размышления

<b>Сервис</b>
/version — версии Claude Code, SDK и бота
/help — эта справка
"""

OWNER_HELP = """

<b>Только для владельца</b>
/users — кто имеет доступ и кто ждёт решения
/grant &lt;user_id&gt; — открыть доступ
/revoke &lt;user_id&gt; — закрыть доступ
/block &lt;user_id&gt; — заблокировать молча
/update — обновить Claude Code и SDK прямо сейчас
"""


@guarded
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = get_app(context)
    user = update.effective_user
    text = HELP
    if user is not None and app.access.is_owner(user.id):
        text += OWNER_HELP
    await reply(update, text)


@guarded
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, context)


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Доступно всем: без user_id владелец не сможет открыть доступ."""
    user = update.effective_user
    chat = update.effective_chat
    if user is None or chat is None:
        return
    await reply(
        update,
        f"🆔 Твой user_id: <code>{user.id}</code>\nЧат: <code>{chat.id}</code>",
    )


@guarded
async def cmd_version(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = get_app(context)
    installed = await updater.cli_version(app.settings.claude_cli)
    latest = await updater.latest_cli_version()
    fresh = "✅ актуальна" if latest and latest in installed else ("🔄 есть новее" if latest else "")
    lines = [
        "<b>Версии</b>",
        f"Claude Code: <code>{installed or 'не найден'}</code> {fresh}",
    ]
    if latest:
        lines.append(f"В npm сейчас: <code>{latest}</code>")
    lines.append(f"claude-agent-sdk: <code>{updater.sdk_version() or '?'}</code>")
    lines.append(f"claude-tg: <code>{updater.bot_version()}</code>")
    await reply(update, "\n".join(lines))


@owner_only
async def cmd_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = get_app(context)
    message = await reply(update, "🔄 Обновляю Claude Code и SDK…")
    try:
        report = await updater.run_update(app.settings.claude_cli)
        text = report.to_text()
    except Exception as exc:  # noqa: BLE001 — отчёт об ошибке полезнее молчания
        log.exception("Обновление не удалось")
        text = f"⚠️ Обновление сорвалось: <code>{html.escape(str(exc))[:400]}</code>"
    await _deliver(update, message, text)


async def _deliver(update: Update, message, text: str) -> None:
    """Показать отчёт, не потеряв его из-за разметки.

    Вывод команд может содержать что угодно, поэтому если Telegram не принял
    HTML — отправляем то же самое без тегов, а не «что-то пошло не так».
    """
    plain = _TAG_RE.sub("", text)
    attempts = []
    if message is not None:
        attempts.append(lambda: message.edit_text(text, parse_mode=ParseMode.HTML))
        attempts.append(lambda: message.edit_text(plain))
    attempts.append(lambda: reply(update, text))
    attempts.append(lambda: reply(update, plain, parse_mode=None))

    for attempt in attempts:
        try:
            await attempt()
            return
        except Exception:  # noqa: BLE001 — пробуем следующий способ
            continue
    log.error("Не смог доставить отчёт об обновлении: %s", plain[:300])


@owner_only
async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = get_app(context)
    approved = await app.access.approved_users()
    pending = await app.access.pending_users()

    lines = ["👥 <b>Доступ к боту</b>", ""]
    lines.append(f"Владелец: <code>{app.settings.owner_id}</code>")
    env_ids = sorted(app.settings.allowed_user_ids - {app.settings.owner_id})
    if env_ids:
        lines.append("Из .env: " + ", ".join(f"<code>{i}</code>" for i in env_ids))
    lines.append("")
    lines.append("<b>Одобрены</b>: " + (", ".join(describe_user(u) for u in approved) or "никого"))
    if pending:
        lines.append("")
        lines.append("<b>Ждут решения</b>:")
        for user in pending:
            lines.append(f"• {describe_user(user)} — /grant {user.user_id}")
    await reply(update, "\n".join(lines))


@owner_only
async def cmd_grant(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = get_app(context)
    user_id = _parse_user_id(context)
    if user_id is None:
        await reply(update, "Как пользоваться: <code>/grant &lt;user_id&gt;</code>")
        return
    await app.access.approve(user_id)
    await reply(update, f"✅ Доступ открыт: <code>{user_id}</code>")
    await _notify_user(context, user_id, "✅ Доступ к боту открыт. Пиши — я на связи.")


@owner_only
async def cmd_revoke(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = get_app(context)
    user_id = _parse_user_id(context)
    if user_id is None:
        await reply(update, "Как пользоваться: <code>/revoke &lt;user_id&gt;</code>")
        return
    await app.access.revoke(user_id)
    await reply(update, f"🚪 Доступ закрыт: <code>{user_id}</code>")


@owner_only
async def cmd_block(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = get_app(context)
    user_id = _parse_user_id(context)
    if user_id is None:
        await reply(update, "Как пользоваться: <code>/block &lt;user_id&gt;</code>")
        return
    await app.access.block(user_id)
    await reply(update, f"⛔️ Заблокирован: <code>{user_id}</code> (сообщения игнорируются)")


async def on_access_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопки «открыть доступ / отказать» под заявкой."""
    query = update.callback_query
    app = get_app(context)
    if query is None or not query.data:
        return
    actor = update.effective_user
    if actor is None or not app.access.is_owner(actor.id):
        await query.answer("Только владелец может решать", show_alert=True)
        return

    _, raw_id, decision = query.data.split(":", 2)
    user_id = int(raw_id)
    if decision == "approve":
        await app.access.approve(user_id)
        await query.answer("Доступ открыт")
        await _edit(query, f"✅ Доступ открыт: <code>{user_id}</code>")
        await _notify_user(context, user_id, "✅ Доступ к боту открыт. Пиши — я на связи.")
    else:
        await app.access.block(user_id)
        await query.answer("Отказано")
        await _edit(query, f"⛔️ Отказано: <code>{user_id}</code>")


async def _edit(query, text: str) -> None:
    try:
        await query.edit_message_text(text, parse_mode=ParseMode.HTML)
    except Exception:  # noqa: BLE001
        log.debug("Не смог обновить сообщение заявки", exc_info=True)


async def _notify_user(context: ContextTypes.DEFAULT_TYPE, user_id: int, text: str) -> None:
    try:
        await context.bot.send_message(chat_id=user_id, text=text)
    except Exception:  # noqa: BLE001 — человек мог не начать диалог
        log.info("Не смог уведомить пользователя %s", user_id)


def _parse_user_id(context: ContextTypes.DEFAULT_TYPE) -> int | None:
    if not context.args:
        return None
    raw = context.args[0].strip().lstrip("@")
    try:
        return int(raw)
    except ValueError:
        return None
