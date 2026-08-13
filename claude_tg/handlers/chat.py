"""Главный обработчик: текст и файлы из Telegram → сессия Claude Code."""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Any

from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import ContextTypes

from ..access import describe_user
from ..app import get_app
from ..bridge import ClaudeSession
from ..streamer import TelegramStreamer
from ..telegram_md import truncate
from .common import context_of, guarded, reply

log = logging.getLogger(__name__)

MAX_OUTBOX_FILES = 5
MAX_OUTBOX_MB = 45
_SAFE_NAME = re.compile(r"[^\w.\-]+", re.UNICODE)


@guarded
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return

    if not _addressed_to_bot(update, context):
        return

    app = get_app(context)
    chat_key, thread_id = context_of(update)

    quota = await app.quota_for(user.id)
    if not quota.allowed:
        await reply(update, quota.refusal())
        await _tell_owner_about_limit(context, user, quota)
        return

    try:
        session = await app.session_for(chat_key, user.id)
    except FileNotFoundError:
        await reply(
            update,
            "⚠️ Не нашёл CLI <code>claude</code>. Установи Claude Code "
            "(<code>npm i -g @anthropic-ai/claude-code</code>) или укажи путь "
            "в <code>CLAUDE_CLI_PATH</code>.",
        )
        return
    except Exception as exc:  # noqa: BLE001 — покажем причину пользователю
        log.exception("Не смог поднять сессию Claude")
        await reply(update, f"⚠️ Не смог запустить Claude: <code>{truncate(str(exc), 300)}</code>")
        return

    if session.busy:
        await reply(update, "⏳ Ещё работаю над прошлым запросом. /stop — прервать.")
        return

    saved = await _save_attachments(update, context, session)
    raw_text = _strip_mention(message.text or message.caption or "", context)
    prompt = _build_prompt(raw_text, saved)
    if not prompt.strip():
        await reply(update, "Пустое сообщение — не понимаю, что сделать.")
        return

    app.set_target(chat_key, message.chat_id, thread_id)
    prefs = await app.prefs_for(chat_key)

    try:
        await context.bot.send_chat_action(
            chat_id=message.chat_id, action=ChatAction.TYPING, message_thread_id=thread_id
        )
    except Exception:  # noqa: BLE001 — необязательная косметика
        pass

    streamer = TelegramStreamer(
        context.bot,
        message.chat_id,
        prefs,
        thread_id=thread_id,
        reply_to=message.message_id,
        limit=app.settings.message_limit,
        interval=app.settings.stream_interval,
    )
    before = _snapshot_outbox(session.workspace)
    await streamer.start()
    result = None
    try:
        result = await session.ask(prompt, streamer)
    except Exception as exc:  # noqa: BLE001 — не роняем бота из-за одного хода
        log.exception("Ход завершился исключением")
        await streamer.on_error(f"⚠️ Сбой: <code>{truncate(str(exc), 300)}</code>")
    finally:
        await streamer.finish()

    if result is not None and result.total_cost_usd:
        await app.record_turn(user.id, result.total_cost_usd, prefs.model)

    await app.remember_session(chat_key, session, user.id)
    await _deliver_outbox(update, context, session, before)


@guarded
async def on_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = get_app(context)
    chat_key, _ = context_of(update)
    session = app.sessions.peek(chat_key)
    if session is None or not session.busy:
        await reply(update, "Сейчас ничего не выполняется.")
        return
    await session.interrupt()
    await reply(update, "⏹ Остановила текущий ход.")


# --- вложения ---------------------------------------------------------------


async def _save_attachments(
    update: Update, context: ContextTypes.DEFAULT_TYPE, session: ClaudeSession
) -> list[str]:
    """Скачать всё присланное в <workspace>/inbox и вернуть относительные пути."""
    message = update.effective_message
    app = get_app(context)
    if message is None:
        return []

    inbox = session.workspace / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    limit_bytes = app.settings.max_download_mb * 1024 * 1024

    candidates: list[tuple[Any, str]] = []
    if message.document:
        candidates.append((message.document, message.document.file_name or "document"))
    if message.photo:
        candidates.append((message.photo[-1], "photo.jpg"))
    if message.voice:
        candidates.append((message.voice, "voice.ogg"))
    if message.audio:
        candidates.append((message.audio, message.audio.file_name or "audio.mp3"))
    if message.video:
        candidates.append((message.video, message.video.file_name or "video.mp4"))
    if message.video_note:
        candidates.append((message.video_note, "video_note.mp4"))

    for tg_file, name in candidates:
        size = getattr(tg_file, "file_size", None) or 0
        if size > limit_bytes:
            await reply(
                update,
                f"⚠️ «{name}» весит {size / 1024 / 1024:.1f} МБ — Telegram отдаёт ботам "
                f"не больше {app.settings.max_download_mb} МБ.",
            )
            continue
        safe = _SAFE_NAME.sub("_", name)[:80] or "file"
        target = inbox / f"{int(time.time())}-{safe}"
        try:
            handle = await tg_file.get_file()
            await handle.download_to_drive(custom_path=str(target))
            saved.append(str(target.relative_to(session.workspace)))
        except Exception:  # noqa: BLE001 — один файл не должен ломать ход
            log.exception("Не смог скачать %s", name)
            await reply(update, f"⚠️ Не удалось скачать «{name}».")

    return saved


async def _tell_owner_about_limit(context: ContextTypes.DEFAULT_TYPE, user, quota) -> None:
    """Владелец должен узнать, что человек упёрся, — иначе тот просто пропадёт."""
    app = get_app(context)
    if app.access.is_owner(user.id):
        return
    try:
        await context.bot.send_message(
            chat_id=app.settings.owner_id,
            text=(
                f"🚦 {describe_user(user)} упёрся в лимит: {quota.describe()}\n"
                f"Поднять: <code>/limit {user.id} 20</code>, снять: "
                f"<code>/limit {user.id} нет</code>"
            ),
            parse_mode=ParseMode.HTML,
        )
    except Exception:  # noqa: BLE001 — уведомление не критично
        log.debug("Не смог сообщить владельцу об упёршемся лимите", exc_info=True)


def _addressed_to_bot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """В личке отвечаем всегда, в группе — только на упоминание или реплай."""
    chat = update.effective_chat
    message = update.effective_message
    if chat is None or message is None:
        return False
    if chat.type == "private":
        return True

    reply_to = message.reply_to_message
    bot_id = context.bot.id
    if reply_to is not None and reply_to.from_user is not None and reply_to.from_user.id == bot_id:
        return True

    username = (context.bot.username or "").lower()
    haystack = f"{message.text or ''} {message.caption or ''}".lower()
    return bool(username) and f"@{username}" in haystack


def _strip_mention(text: str, context: ContextTypes.DEFAULT_TYPE) -> str:
    username = context.bot.username
    if not username:
        return text
    return re.sub(rf"@{re.escape(username)}\b", "", text, flags=re.IGNORECASE).strip()


def _build_prompt(text: str, saved: list[str]) -> str:
    if not saved:
        return text
    listing = "\n".join(f"- {path}" for path in saved)
    note = (
        "Пользователь прислал файлы, они уже лежат в рабочей папке "
        f"(пути относительно cwd):\n{listing}"
    )
    return f"{text}\n\n{note}".strip() if text else note


# --- отдача результатов -----------------------------------------------------


def _snapshot_outbox(workspace: Path) -> dict[str, float]:
    outbox = workspace / "outbox"
    if not outbox.exists():
        return {}
    return {
        str(p): p.stat().st_mtime
        for p in outbox.rglob("*")
        if p.is_file()
    }


async def _deliver_outbox(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session: ClaudeSession,
    before: dict[str, float],
) -> None:
    """Всё, что Claude положил в outbox/, отправляем файлами в чат."""
    after = _snapshot_outbox(session.workspace)
    fresh = [Path(p) for p, mtime in after.items() if before.get(p) != mtime]
    if not fresh:
        return

    message = update.effective_message
    if message is None:
        return
    _, thread_id = context_of(update)

    for path in sorted(fresh)[:MAX_OUTBOX_FILES]:
        try:
            if path.stat().st_size > MAX_OUTBOX_MB * 1024 * 1024:
                await reply(update, f"📦 <code>{path.name}</code> слишком большой для Telegram.")
                continue
            with path.open("rb") as handle:
                await context.bot.send_document(
                    chat_id=message.chat_id,
                    document=handle,
                    filename=path.name,
                    message_thread_id=thread_id,
                )
        except Exception:  # noqa: BLE001
            log.exception("Не смог отправить файл %s", path)

    if len(fresh) > MAX_OUTBOX_FILES:
        await reply(
            update,
            f"📦 Ещё {len(fresh) - MAX_OUTBOX_FILES} файл(ов) остались в "
            f"<code>outbox/</code> рабочей папки.",
        )
