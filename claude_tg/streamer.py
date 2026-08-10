"""Живой рендер ответа Claude в сообщения Telegram.

Пока Claude пишет — мы правим одно сообщение раз в ~1.5 с. Когда текст
перестаёт помещаться, текущее сообщение «замораживается» и открывается новое.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

from telegram import LinkPreviewOptions, Message
from telegram.constants import ParseMode
from telegram.error import BadRequest, RetryAfter, TimedOut

from . import catalog
from .bridge import TurnListener
from .storage import Prefs
from .telegram_md import md_to_html, quote_block, split_markdown, truncate

log = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")

TELEGRAM_SAFE_LIMIT = 4000  # у Telegram лимит 4096, оставляем запас на теги
THINKING_TAIL = 1200
TOOL_LINES = 6
TOOL_ARG_LIMIT = 110

_TOOL_ICONS = {
    "Bash": "🖥",
    "Read": "📖",
    "Write": "✍️",
    "Edit": "✏️",
    "Glob": "🔎",
    "Grep": "🔍",
    "WebSearch": "🌐",
    "WebFetch": "🌐",
    "web_search": "🌐",
    "web_fetch": "🌐",
    "Task": "🤖",
    "TodoWrite": "🗒",
    "NotebookEdit": "📓",
    "Skill": "🧩",
    "code_execution": "🧮",
}


def tool_icon(name: str) -> str:
    return _TOOL_ICONS.get(name, "⚙️")


def describe_tool(name: str, tool_input: dict[str, Any]) -> str:
    """Короткая человекочитаемая подпись вызова инструмента."""
    if not isinstance(tool_input, dict):
        return name
    for key in ("command", "file_path", "path", "pattern", "query", "url", "description", "prompt"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return f"{name}: {truncate(value.replace(chr(10), ' '), TOOL_ARG_LIMIT)}"
    return name


class TelegramStreamer(TurnListener):
    """Собирает ответ и держит его отрисованным в чате."""

    def __init__(
        self,
        bot,
        chat_id: int,
        prefs: Prefs,
        *,
        thread_id: int | None = None,
        reply_to: int | None = None,
        limit: int = 3500,
        interval: float = 1.4,
    ) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.thread_id = thread_id
        self.reply_to = reply_to
        self.prefs = prefs
        self.limit = limit
        self.interval = interval

        self.text = ""
        self.thinking = ""
        self.tool_lines: list[str] = []
        self.notes: list[str] = []
        self.errors: list[str] = []
        self.result: Any = None

        self._messages: list[Message] = []
        self._rendered: list[str] = []
        self._status = "🧠 Думаю…"
        self._last_flush = 0.0
        self._dirty = False
        self._flush_lock = asyncio.Lock()
        self._pending_tools: dict[str, str] = {}

    # --- публичный API ------------------------------------------------------

    async def start(self) -> None:
        await self._ensure_first_message()

    async def finish(self) -> None:
        """Финальный рендер: убрать статус, добавить футер."""
        self._status = ""
        self._dirty = True
        await self._flush(force=True, final=True)

    @property
    def produced_text(self) -> str:
        return self.text

    # --- события ------------------------------------------------------------

    async def on_text(self, delta: str) -> None:
        self.text += delta
        self._status = "✍️ Пишу…"
        await self._maybe_flush()

    async def on_thinking(self, delta: str) -> None:
        self.thinking += delta
        self._status = "🧠 Думаю…"
        if self.prefs.show_thinking:
            await self._maybe_flush()

    async def on_tool_use(self, tool_id: str, name: str, tool_input: dict[str, Any]) -> None:
        line = f"{tool_icon(name)} {describe_tool(name, tool_input)}"
        self._pending_tools[tool_id] = line
        self.tool_lines.append(line)
        self._status = f"{tool_icon(name)} {name}…"
        if self.prefs.show_tools:
            await self._maybe_flush()

    async def on_tool_result(self, tool_id: str, is_error: bool, preview: str) -> None:
        if not is_error:
            self._pending_tools.pop(tool_id, None)
            return
        line = self._pending_tools.pop(tool_id, "инструмент")
        self.tool_lines.append(f"⚠️ ошибка · {truncate(preview or line, 160)}")
        if self.prefs.show_tools:
            await self._maybe_flush()

    async def on_system(self, subtype: str, data: dict[str, Any]) -> None:
        if subtype == "compact_boundary":
            self.notes.append("🗜 Контекст сжат, продолжаю.")
            await self._maybe_flush()

    async def on_task(self, text: str) -> None:
        self.notes.append(f"🤖 {truncate(text, 200)}")
        await self._maybe_flush()

    async def on_rate_limit(self, info: Any) -> None:
        status = getattr(info, "status", None)
        if status in {"allowed_warning", "rejected"}:
            util = getattr(info, "utilization", None)
            pct = f" ({util:.0%})" if isinstance(util, (int, float)) else ""
            self.notes.append(f"📉 Лимит подписки: {status}{pct}")
            await self._maybe_flush()

    async def on_error(self, message: str) -> None:
        self.errors.append(message)
        await self._maybe_flush(force=True)

    async def on_result(self, result: Any) -> None:
        self.result = result
        if getattr(result, "is_error", False):
            detail = getattr(result, "result", None) or getattr(result, "terminal_reason", None)
            self.errors.append(f"⚠️ {truncate(str(detail or 'ход завершился ошибкой'), 400)}")

    # --- рендер -------------------------------------------------------------

    def _prefix_html(self, final: bool) -> str:
        """Шапка первой страницы: статус, размышления, вызовы инструментов, заметки."""
        parts: list[str] = []

        if self._status and not final:
            parts.append(md_to_html(self._status))

        if self.prefs.show_thinking and self.thinking.strip():
            tail = self.thinking.strip()[-THINKING_TAIL:]
            parts.append("🧠 <i>Размышления</i>\n" + quote_block(tail))

        if self.prefs.show_tools and self.tool_lines:
            shown = "\n".join(self.tool_lines[-TOOL_LINES:])
            parts.append(quote_block(shown, expandable=False))

        if self.notes:
            parts.append(md_to_html("\n".join(self.notes[-3:])))

        return "\n\n".join(parts)

    def _suffix_html(self, final: bool) -> str:
        """Подвал последней страницы: ошибки и футер с моделью/effort."""
        parts: list[str] = []
        if self.errors:
            parts.append(md_to_html("\n".join(self.errors[-3:])))
        if final and self.prefs.show_footer:
            footer = self._footer()
            if footer:
                parts.append(f"<i>{md_to_html(footer)}</i>")
        return "\n\n".join(parts)

    def _footer(self) -> str:
        model = self.prefs.model
        bits = [f"{catalog.model_emoji(model)} {model}", f"⚡ {self.prefs.effort}"]
        result = self.result
        if result is not None:
            duration = getattr(result, "duration_ms", 0) or 0
            if duration:
                bits.append(f"{duration / 1000:.1f} c")
            cost = getattr(result, "total_cost_usd", None)
            if cost:
                bits.append(f"${cost:.4f}")
        return "— " + " · ".join(bits)

    def _pages(self, final: bool) -> list[str]:
        """Готовые HTML-страницы: шапка на первой, подвал на последней."""
        prefix = self._prefix_html(final)
        suffix = self._suffix_html(final)
        reserved = len(prefix) + len(suffix) + 48
        body_limit = max(800, min(self.limit, TELEGRAM_SAFE_LIMIT - reserved))

        body = self.text.strip()
        if not body and not final:
            body = "…" if not prefix else ""
        chunks = split_markdown(body, body_limit) if body else [""]

        pages: list[str] = []
        for index, chunk in enumerate(chunks):
            blocks: list[str] = []
            if index == 0 and prefix:
                blocks.append(prefix)
            if chunk:
                blocks.append(md_to_html(chunk))
            if index == len(chunks) - 1 and suffix:
                blocks.append(suffix)
            pages.append("\n\n".join(b for b in blocks if b))
        return [p for p in pages if p] or ["…"]

    async def _maybe_flush(self, force: bool = False) -> None:
        self._dirty = True
        now = time.monotonic()
        if force or now - self._last_flush >= self.interval:
            await self._flush(force=force)

    async def _flush(self, force: bool = False, final: bool = False) -> None:
        if not self._dirty and not force:
            return
        async with self._flush_lock:
            self._dirty = False
            self._last_flush = time.monotonic()

            for index, rendered in enumerate(self._pages(final)):
                if index < len(self._messages):
                    if index < len(self._rendered) and self._rendered[index] == rendered:
                        continue
                    if await self._edit(self._messages[index], rendered):
                        self._set_rendered(index, rendered)
                else:
                    message = await self._send(rendered)
                    if message is not None:
                        self._messages.append(message)
                        self._set_rendered(index, rendered)

    def _set_rendered(self, index: int, rendered: str) -> None:
        while len(self._rendered) <= index:
            self._rendered.append("")
        self._rendered[index] = rendered

    async def _ensure_first_message(self) -> None:
        if self._messages:
            return
        message = await self._send("🧠 Думаю…")
        if message is not None:
            self._messages.append(message)
            self._set_rendered(0, "🧠 Думаю…")

    async def _send(self, html_text: str) -> Message | None:
        kwargs: dict[str, Any] = {
            "chat_id": self.chat_id,
            "text": html_text or "…",
            "parse_mode": ParseMode.HTML,
            "link_preview_options": LinkPreviewOptions(is_disabled=True),
        }
        if self.thread_id is not None:
            kwargs["message_thread_id"] = self.thread_id
        if not self._messages and self.reply_to is not None:
            kwargs["reply_to_message_id"] = self.reply_to
        return await self._call(lambda: self.bot.send_message(**kwargs))

    async def _edit(self, message: Message, html_text: str) -> bool:
        result = await self._call(
            lambda: self.bot.edit_message_text(
                chat_id=self.chat_id,
                message_id=message.message_id,
                text=html_text or "…",
                parse_mode=ParseMode.HTML,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        )
        return result is not None

    async def _call(self, factory):
        """Отправка с уважением к флуд-контролю и без падений на мелочах."""
        for attempt in range(4):
            try:
                return await factory()
            except RetryAfter as exc:
                await asyncio.sleep(float(exc.retry_after) + 0.5)
            except BadRequest as exc:
                text = str(exc).lower()
                if "not modified" in text:
                    return None
                if "parse entities" in text or "unsupported start tag" in text:
                    log.warning("HTML не принят Telegram, шлю как обычный текст: %s", exc)
                    return await self._fallback_plain()
                log.warning("BadRequest от Telegram: %s", exc)
                return None
            except TimedOut:
                await asyncio.sleep(1.0 + attempt)
            except Exception:  # noqa: BLE001 — сеть, отозванные права и т.п.
                log.exception("Не смог отправить сообщение в Telegram")
                return None
        return None

    async def _fallback_plain(self):
        """Если Telegram не принял разметку — отдаём голый текст без тегов."""
        plain = _TAG_RE.sub("", self.text.strip() or "…")
        kwargs: dict[str, Any] = {
            "chat_id": self.chat_id,
            "text": truncate(plain, self.limit) or "…",
            "link_preview_options": LinkPreviewOptions(is_disabled=True),
        }
        if self.thread_id is not None:
            kwargs["message_thread_id"] = self.thread_id
        try:
            return await self.bot.send_message(**kwargs)
        except Exception:  # noqa: BLE001
            log.exception("Даже plain-text не ушёл")
            return None
