"""Управление сессиями Claude: новая, список, продолжить, ответвить, статус."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from pathlib import Path

from claude_agent_sdk import fork_session, list_sessions
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from .. import catalog, ui
from ..app import AppContext, get_app
from ..bridge import ClaudeSession
from ..telegram_md import truncate
from .common import context_of, guarded, reply

log = logging.getLogger(__name__)

SESSION_LIST_LIMIT = 10


async def status_text(app: AppContext, chat_key: str, session: ClaudeSession | None) -> str:
    prefs = await app.prefs_for(chat_key)
    stored = await app.storage.get_chat_session(chat_key)
    lines = ["📊 <b>Статус сессии</b>", ""]
    lines.append(f"🧠 {catalog.model_emoji(prefs.model)} <code>{prefs.model}</code> · ⚡ {prefs.effort}")
    lines.append(f"🎛 {catalog.label('mode', prefs.permission_mode)}")
    if stored and stored.session_id:
        lines.append(f"🧵 Сессия: <code>{stored.session_id}</code>")
    else:
        lines.append("🧵 Сессия ещё не создана")
    if stored:
        lines.append(f"📁 Папка: <code>{stored.workspace}</code>")

    if session is not None:
        lines.append("")
        lines.append(f"Ходов за запуск: {session.stats.turns}")
        if session.stats.total_cost_usd:
            lines.append(f"Потрачено: ${session.stats.total_cost_usd:.4f}")
        if session.session_allow:
            lines.append("Разрешено всегда: " + ", ".join(sorted(session.session_allow)))
        usage = await session.context_usage()
        if usage:
            total = usage.get("totalTokens")
            maximum = usage.get("maxTokens")
            pct = usage.get("percentage")
            if isinstance(total, int) and isinstance(maximum, int) and maximum:
                bar = _bar(total / maximum)
                pct_text = f"{pct:.0f}%" if isinstance(pct, (int, float)) else ""
                lines.append(f"Контекст: {bar} {total:,} / {maximum:,} {pct_text}".replace(",", " "))
    return "\n".join(lines)


def _seconds(stamp: int | float) -> float:
    """CLI отдаёт время то в секундах, то в миллисекундах — приводим к секундам."""
    return stamp / 1000 if stamp > 1e11 else float(stamp)


def _bar(fraction: float, width: int = 10) -> str:
    filled = max(0, min(width, round(fraction * width)))
    return "▰" * filled + "▱" * (width - filled)


@guarded
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = get_app(context)
    chat_key, _ = context_of(update)
    await reply(update, await status_text(app, chat_key, app.sessions.peek(chat_key)))


@guarded
async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = get_app(context)
    user = update.effective_user
    chat_key, _ = context_of(update)
    if user is None:
        return

    await app.sessions.drop(chat_key)
    stored = await app.storage.get_chat_session(chat_key)
    workspace = Path(stored.workspace) if stored else app.sessions.workspace_for(user.id, chat_key)
    await app.storage.save_chat_session(chat_key, str(workspace), None, user.id)
    await reply(
        update,
        "🆕 Начали новую сессию. Контекст прошлой сохранён — <code>/sessions</code>, "
        "чтобы вернуться.",
    )


@guarded
async def cmd_sessions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = get_app(context)
    user = update.effective_user
    chat_key, _ = context_of(update)
    if user is None:
        return

    stored = await app.storage.get_chat_session(chat_key)
    workspace = Path(stored.workspace) if stored else app.sessions.workspace_for(user.id, chat_key)
    try:
        items = await _list_sessions(str(workspace))
    except Exception as exc:  # noqa: BLE001 — история могла не создаться
        log.debug("list_sessions упал", exc_info=True)
        await reply(update, f"Пока нет сохранённых сессий ({truncate(str(exc), 120)}).")
        return

    if not items:
        await reply(update, "Пока нет сохранённых сессий в этой папке.")
        return

    lines = ["📚 <b>Сессии этого чата</b>", ""]
    buttons: list[list[InlineKeyboardButton]] = []
    current = stored.session_id if stored else None
    for index, info in enumerate(items[:SESSION_LIST_LIMIT], start=1):
        when = dt.datetime.fromtimestamp(_seconds(info.last_modified))
        title = info.custom_title or info.summary or info.first_prompt or "без названия"
        mark = " ← сейчас" if info.session_id == current else ""
        lines.append(f"{index}. {truncate(title, 70)} · {when:%d.%m %H:%M}{mark}")
        buttons.append(
            [InlineKeyboardButton(f"↩️ {index}", callback_data=f"sres:{info.session_id}")]
        )

    await reply(
        update,
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(buttons) if buttons else None,
    )


async def _list_sessions(directory: str):
    return await asyncio.to_thread(list_sessions, directory, SESSION_LIST_LIMIT, 0)


@guarded
async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await reply(update, "Как пользоваться: <code>/resume &lt;session-id&gt;</code> (id из /sessions)")
        return
    await _switch_to(update, context, context.args[0].strip())


@guarded
async def cmd_fork(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ответвить копию текущей сессии — эксперименты без порчи основной ветки."""
    app = get_app(context)
    user = update.effective_user
    chat_key, _ = context_of(update)
    if user is None:
        return

    stored = await app.storage.get_chat_session(chat_key)
    if not stored or not stored.session_id:
        await reply(update, "Нечего ответвлять — сессия ещё не начата.")
        return

    try:
        result = await asyncio.to_thread(fork_session, stored.session_id, stored.workspace, None, None)
    except Exception as exc:  # noqa: BLE001
        log.exception("fork_session упал")
        await reply(update, f"⚠️ Не смогла ответвить: <code>{truncate(str(exc), 200)}</code>")
        return

    new_id = getattr(result, "session_id", None) or getattr(result, "new_session_id", None)
    if not new_id:
        await reply(update, "⚠️ CLI не вернул id новой сессии.")
        return
    await _switch_to(update, context, str(new_id), note="🌿 Ответвила копию сессии.")


async def _switch_to(
    update: Update, context: ContextTypes.DEFAULT_TYPE, session_id: str, note: str = ""
) -> None:
    app = get_app(context)
    user = update.effective_user
    chat_key, _ = context_of(update)
    if user is None:
        return

    stored = await app.storage.get_chat_session(chat_key)
    workspace = Path(stored.workspace) if stored else app.sessions.workspace_for(user.id, chat_key)

    await app.sessions.drop(chat_key)
    await app.storage.save_chat_session(chat_key, str(workspace), session_id, user.id)
    try:
        await app.session_for(chat_key, user.id)
    except Exception as exc:  # noqa: BLE001
        log.exception("Не смог продолжить сессию")
        await reply(update, f"⚠️ Не смогла открыть сессию: <code>{truncate(str(exc), 200)}</code>")
        return
    await reply(update, f"{note}\n▶️ Продолжаю сессию <code>{session_id}</code>".strip())


@guarded
async def cmd_workspace(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = get_app(context)
    user = update.effective_user
    chat_key, _ = context_of(update)
    if user is None:
        return
    stored = await app.storage.get_chat_session(chat_key)
    workspace = Path(stored.workspace) if stored else app.sessions.workspace_for(user.id, chat_key)
    workspace.mkdir(parents=True, exist_ok=True)

    entries = sorted(p for p in workspace.rglob("*") if p.is_file())[:30]
    listing = "\n".join(f"• {p.relative_to(workspace)}" for p in entries) or "пусто"
    await reply(
        update,
        f"📁 Рабочая папка:\n<code>{workspace}</code>\n\n{listing}\n\n"
        "Файлы, положенные Claude в <code>outbox/</code>, я пришлю в чат автоматически.",
    )


@guarded
async def on_session_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """sess:* и sres:<id> — кнопки управления сессией."""
    query = update.callback_query
    if query is None or not query.data:
        return
    app = get_app(context)
    chat_key, _ = context_of(update)

    if query.data.startswith("sres:"):
        await query.answer()
        await _switch_to(update, context, query.data.split(":", 1)[1])
        return

    action = query.data.split(":", 1)[1]
    if action == "stop":
        session = app.sessions.peek(chat_key)
        if session is not None:
            await session.interrupt()
        await query.answer("Остановила")
        return
    if action == "new":
        await query.answer()
        await cmd_new(update, context)
        return
    if action == "fork":
        await query.answer()
        await cmd_fork(update, context)
        return
    if action == "restart":
        session = app.sessions.peek(chat_key)
        if session is not None:
            await session.start(resume=session.session_id)
        await query.answer("Перезапустила процесс")
        return
    if action == "list":
        await query.answer()
        await cmd_sessions(update, context)
        return
    await query.answer()


@guarded
async def cmd_sessions_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = get_app(context)
    chat_key, _ = context_of(update)
    stored = await app.storage.get_chat_session(chat_key)
    await reply(
        update,
        "🧵 Что сделать с сессией?",
        reply_markup=ui.sessions_menu(bool(stored and stored.session_id)),
    )
