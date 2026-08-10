"""Markdown от Claude → безопасный HTML для Telegram + аккуратная нарезка.

Telegram принимает крошечное подмножество HTML, а MarkdownV2 требует
экранировать почти всё. Поэтому: режем исходный markdown на куски по границам
строк (не ломая ``` блоки), а затем каждый кусок рендерим независимо.
"""

from __future__ import annotations

import html
import re

TELEGRAM_LIMIT = 4096

_FENCE_RE = re.compile(r"^\s*```(\S*)\s*$")
_CODE_SPAN_RE = re.compile(r"`([^`\n]+)`")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_BOLD_ALT_RE = re.compile(r"(?<![\w*])__(.+?)__(?![\w*])", re.DOTALL)
_ITALIC_RE = re.compile(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])")
_ITALIC_ALT_RE = re.compile(r"(?<![\w_])_(?!\s)([^_\n]+?)(?<!\s)_(?![\w_])")
_STRIKE_RE = re.compile(r"~~(.+?)~~", re.DOTALL)
_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET_RE = re.compile(r"^(\s*)[-*+]\s+")


def split_markdown(text: str, limit: int = 3500) -> list[str]:
    """Разбить markdown на куски ≤ limit, не разрывая ``` блоки.

    Если блок кода не влезает целиком — он делится, но каждая часть получает
    свою пару ``` , чтобы подсветка не «утекала» в соседнее сообщение.
    """
    if limit <= 0:
        raise ValueError("limit должен быть > 0")
    if len(text) <= limit:
        return [text] if text else []

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    fence_lang: str | None = None  # язык открытого блока, если мы внутри него

    def flush() -> None:
        nonlocal current, current_len
        if not current:
            return
        body = "\n".join(current)
        if fence_lang is not None:
            body += "\n```"
        chunks.append(body)
        current = []
        current_len = 0
        if fence_lang is not None:
            reopen = f"```{fence_lang}"
            current.append(reopen)
            current_len = len(reopen) + 1

    for raw_line in text.split("\n"):
        for line in _hard_wrap(raw_line, limit - 8):
            fence = _FENCE_RE.match(line)
            addition = len(line) + 1
            if current_len + addition > limit and current:
                flush()
            current.append(line)
            current_len += addition
            if fence:
                fence_lang = None if fence_lang is not None else (fence.group(1) or "")

    if current:
        body = "\n".join(current)
        if fence_lang is not None:
            body += "\n```"
        chunks.append(body)

    return [c for c in (chunk.strip("\n") for chunk in chunks) if c]


def _hard_wrap(line: str, width: int) -> list[str]:
    """Аварийно порезать сверхдлинную строку (например, base64 в одну строку)."""
    if len(line) <= width:
        return [line]
    return [line[i : i + width] for i in range(0, len(line), width)]


def md_to_html(text: str) -> str:
    """Отрендерить markdown-кусок в HTML, который переварит Telegram."""
    placeholders: list[str] = []

    def stash(rendered: str) -> str:
        placeholders.append(rendered)
        return f"\x00{len(placeholders) - 1}\x00"

    lines = text.split("\n")
    out: list[str] = []
    in_fence = False
    fence_lang = ""
    fence_body: list[str] = []

    for line in lines:
        fence = _FENCE_RE.match(line)
        if fence:
            if in_fence:
                out.append(stash(_render_code_block(fence_lang, "\n".join(fence_body))))
                in_fence = False
                fence_lang = ""
                fence_body = []
            else:
                in_fence = True
                fence_lang = fence.group(1) or ""
            continue
        if in_fence:
            fence_body.append(line)
            continue
        out.append(_render_line(line, stash))

    if in_fence:  # незакрытый блок — всё равно показываем как код
        out.append(stash(_render_code_block(fence_lang, "\n".join(fence_body))))

    body = "\n".join(out)
    body = html.escape(body, quote=False)
    for idx, rendered in enumerate(placeholders):
        body = body.replace(f"\x00{idx}\x00", rendered)
    return body


def _render_code_block(lang: str, code: str) -> str:
    escaped = html.escape(code, quote=False)
    if lang:
        safe_lang = re.sub(r"[^\w+.#-]", "", lang)[:24]
        return f'<pre><code class="language-{safe_lang}">{escaped}</code></pre>'
    return f"<pre>{escaped}</pre>"


def _render_line(line: str, stash) -> str:
    heading = _HEADING_RE.match(line)
    if heading:
        line = heading.group(2)

    line = _BULLET_RE.sub(lambda m: f"{m.group(1)}• ", line)

    line = _CODE_SPAN_RE.sub(lambda m: stash(f"<code>{html.escape(m.group(1), quote=False)}</code>"), line)
    line = _LINK_RE.sub(
        lambda m: stash(
            f'<a href="{html.escape(m.group(2), quote=True)}">'
            f"{html.escape(m.group(1), quote=False)}</a>"
        ),
        line,
    )
    line = _BOLD_RE.sub(lambda m: stash(f"<b>{html.escape(m.group(1), quote=False)}</b>"), line)
    line = _BOLD_ALT_RE.sub(lambda m: stash(f"<b>{html.escape(m.group(1), quote=False)}</b>"), line)
    line = _STRIKE_RE.sub(lambda m: stash(f"<s>{html.escape(m.group(1), quote=False)}</s>"), line)
    line = _ITALIC_RE.sub(lambda m: stash(f"<i>{html.escape(m.group(1), quote=False)}</i>"), line)
    line = _ITALIC_ALT_RE.sub(lambda m: stash(f"<i>{html.escape(m.group(1), quote=False)}</i>"), line)

    if heading:
        line = stash("<b>") + line + stash("</b>")
    return line


def quote_block(text: str, expandable: bool = True) -> str:
    """Свернуть текст в цитату (для размышлений и логов инструментов)."""
    escaped = html.escape(text, quote=False)
    tag = "<blockquote expandable>" if expandable else "<blockquote>"
    return f"{tag}{escaped}</blockquote>"


def truncate(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"
