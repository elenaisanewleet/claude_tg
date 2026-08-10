"""Мост между Telegram и Claude Code — обёртка над claude-agent-sdk.

Одна `ClaudeSession` = один живой процесс `claude` со своей рабочей папкой,
своей сессией (её id переживает перезапуски) и своими настройками.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    RateLimitEvent,
    ResultMessage,
    ServerToolUseBlock,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolPermissionContext,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from . import catalog
from .storage import Prefs

log = logging.getLogger(__name__)

PermissionCallback = Callable[
    ["ClaudeSession", str, dict[str, Any], ToolPermissionContext],
    Awaitable[PermissionResultAllow | PermissionResultDeny],
]

# Настройки, которые нельзя поменять на лету — нужен перезапуск процесса.
RESTART_KEYS = ("effort", "thinking", "allowed_tools")


class TurnListener:
    """Приёмник событий одного хода. Все методы — no-op, переопределяй нужные."""

    async def on_text(self, delta: str) -> None: ...

    async def on_thinking(self, delta: str) -> None: ...

    async def on_tool_use(self, tool_id: str, name: str, tool_input: dict[str, Any]) -> None: ...

    async def on_tool_result(self, tool_id: str, is_error: bool, preview: str) -> None: ...

    async def on_system(self, subtype: str, data: dict[str, Any]) -> None: ...

    async def on_task(self, text: str) -> None: ...

    async def on_rate_limit(self, info: Any) -> None: ...

    async def on_error(self, message: str) -> None: ...

    async def on_result(self, result: ResultMessage) -> None: ...


@dataclass
class SessionStats:
    """Что показываем в футере и в /status."""

    last_model: str = ""
    last_cost_usd: float | None = None
    last_duration_ms: int = 0
    total_cost_usd: float = 0.0
    turns: int = 0


class ClaudeSession:
    """Живая сессия Claude Code, привязанная к чату/треду Telegram."""

    def __init__(
        self,
        chat_key: str,
        workspace: Path,
        prefs: Prefs,
        *,
        cli_path: str | None = None,
        permission_cb: PermissionCallback | None = None,
        session_id: str | None = None,
        turn_timeout: int = 3600,
    ) -> None:
        self.chat_key = chat_key
        self.workspace = workspace
        self.prefs = prefs
        self.cli_path = cli_path
        self.permission_cb = permission_cb
        self.session_id = session_id
        self.turn_timeout = turn_timeout

        self.stats = SessionStats()
        self.available_tools: list[str] = []
        self.mcp_servers: list[dict[str, Any]] = []
        self.session_allow: set[str] = set()  # «разрешать всегда» в рамках сессии

        self._client: ClaudeSDKClient | None = None
        self._lock = asyncio.Lock()
        self._streamed = ""
        self._busy = False

    # --- жизненный цикл -----------------------------------------------------

    def _build_options(self, resume: str | None) -> ClaudeAgentOptions:
        self.workspace.mkdir(parents=True, exist_ok=True)
        (self.workspace / "inbox").mkdir(exist_ok=True)
        (self.workspace / "outbox").mkdir(exist_ok=True)

        return ClaudeAgentOptions(
            cwd=str(self.workspace),
            cli_path=self.cli_path,
            model=self.prefs.model,
            effort=self.prefs.effort,  # type: ignore[arg-type]
            thinking=catalog.thinking_config(self.prefs.thinking, self.prefs.effort),  # type: ignore[arg-type]
            permission_mode=self.prefs.permission_mode,  # type: ignore[arg-type]
            allowed_tools=list(self.prefs.allowed_tools),
            can_use_tool=self._on_permission_request,
            include_partial_messages=True,
            enable_file_checkpointing=True,
            # Ведём себя как настоящий Claude Code: пресетный системный промпт,
            # полный набор инструментов, скиллы и пользовательские настройки.
            system_prompt={"type": "preset", "preset": "claude_code"},
            tools={"type": "preset", "preset": "claude_code"},
            skills="all",
            setting_sources=["user", "project", "local"],
            resume=resume,
            fork_session=False,
        )

    async def start(self, resume: str | None = None) -> None:
        """Поднять процесс `claude`. `resume` — id сессии, которую продолжаем."""
        await self.close()
        resume = resume if resume is not None else self.session_id
        client = ClaudeSDKClient(options=self._build_options(resume))
        await client.connect()
        self._client = client
        try:
            info = await client.get_server_info()
            if info:
                self._absorb_server_info(info)
        except Exception:  # noqa: BLE001 — информационный вызов, не критичный
            log.debug("get_server_info недоступен", exc_info=True)

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is None:
            return
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001 — процесс мог умереть сам
            log.debug("disconnect упал", exc_info=True)

    @property
    def connected(self) -> bool:
        return self._client is not None

    @property
    def busy(self) -> bool:
        return self._busy

    def _absorb_server_info(self, info: dict[str, Any]) -> None:
        tools = info.get("tools") or info.get("availableTools")
        if isinstance(tools, list):
            self.available_tools = [t if isinstance(t, str) else str(t.get("name")) for t in tools]
        servers = info.get("mcp_servers") or info.get("mcpServers")
        if isinstance(servers, list):
            self.mcp_servers = [s for s in servers if isinstance(s, dict)]
        sid = info.get("session_id") or info.get("sessionId")
        if isinstance(sid, str) and sid:
            self.session_id = sid

    # --- разрешения ---------------------------------------------------------

    async def _on_permission_request(
        self, tool_name: str, tool_input: dict[str, Any], context: ToolPermissionContext
    ) -> PermissionResultAllow | PermissionResultDeny:
        if tool_name in self.session_allow:
            return PermissionResultAllow(updated_input=tool_input)
        if self.permission_cb is None:
            return PermissionResultAllow(updated_input=tool_input)
        return await self.permission_cb(self, tool_name, tool_input, context)

    # --- один ход -----------------------------------------------------------

    async def ask(self, prompt: str, listener: TurnListener) -> ResultMessage | None:
        """Отправить сообщение и прогнать все события ответа через listener."""
        if self._client is None:
            await self.start()
        assert self._client is not None

        async with self._lock:
            self._busy = True
            self._streamed = ""
            result: ResultMessage | None = None
            try:
                await self._client.query(prompt)
                async with asyncio.timeout(self.turn_timeout):
                    async for message in self._client.receive_response():
                        maybe = await self._dispatch(message, listener)
                        if maybe is not None:
                            result = maybe
            except TimeoutError:
                await listener.on_error(
                    f"⏳ Ход не завершился за {self.turn_timeout // 60} мин — прерываю."
                )
                await self.interrupt()
            finally:
                self._busy = False
            return result

    async def _dispatch(self, message: Any, listener: TurnListener) -> ResultMessage | None:
        sid = getattr(message, "session_id", None)
        if isinstance(sid, str) and sid:
            self.session_id = sid

        if isinstance(message, StreamEvent):
            await self._dispatch_stream_event(message, listener)
            return None

        if isinstance(message, AssistantMessage):
            if message.error:
                await listener.on_error(f"Ошибка API: {message.error}")
            for block in message.content:
                if isinstance(block, TextBlock):
                    if block.text and block.text not in self._streamed:
                        self._streamed += block.text
                        await listener.on_text(block.text)
                elif isinstance(block, ThinkingBlock):
                    if block.thinking and block.thinking not in self._streamed:
                        self._streamed += block.thinking
                        await listener.on_thinking(block.thinking)
                elif isinstance(block, ToolUseBlock):
                    await listener.on_tool_use(block.id, block.name, block.input)
                elif isinstance(block, ServerToolUseBlock):
                    await listener.on_tool_use(block.id, block.name, block.input)
            if message.model:
                self.stats.last_model = message.model
            return None

        if isinstance(message, UserMessage):
            if isinstance(message.content, list):
                for block in message.content:
                    if isinstance(block, ToolResultBlock):
                        await listener.on_tool_result(
                            block.tool_use_id,
                            bool(block.is_error),
                            _preview_tool_result(block.content),
                        )
            return None

        if isinstance(message, SystemMessage):
            if message.subtype == "init":
                self._absorb_server_info(message.data)
            await listener.on_system(message.subtype, message.data)
            return None

        if isinstance(message, RateLimitEvent):
            await listener.on_rate_limit(message.rate_limit_info)
            return None

        if isinstance(message, ResultMessage):
            self.stats.turns += 1
            self.stats.last_duration_ms = message.duration_ms
            self.stats.last_cost_usd = message.total_cost_usd
            if message.total_cost_usd:
                self.stats.total_cost_usd += message.total_cost_usd
            if message.session_id:
                self.session_id = message.session_id
            await listener.on_result(message)
            return message

        # Фоновые задачи (Task tool) и прочие уведомления
        text = getattr(message, "summary", None) or getattr(message, "description", None)
        if text:
            await listener.on_task(str(text))
        return None

    async def _dispatch_stream_event(self, message: StreamEvent, listener: TurnListener) -> None:
        event = message.event or {}
        etype = event.get("type")
        if etype == "content_block_delta":
            delta = event.get("delta") or {}
            dtype = delta.get("type")
            if dtype == "text_delta":
                text = delta.get("text") or ""
                if text:
                    self._streamed += text
                    await listener.on_text(text)
            elif dtype == "thinking_delta":
                text = delta.get("thinking") or ""
                if text:
                    self._streamed += text
                    await listener.on_thinking(text)

    # --- управление ---------------------------------------------------------

    async def interrupt(self) -> None:
        if self._client is None:
            return
        try:
            await self._client.interrupt()
        except Exception:  # noqa: BLE001 — прерывать нечего
            log.debug("interrupt упал", exc_info=True)

    async def apply_prefs(self, new: Prefs) -> bool:
        """Применить настройки. Возвращает True, если пришлось перезапустить процесс."""
        old, self.prefs = self.prefs, new
        needs_restart = any(getattr(old, key) != getattr(new, key) for key in RESTART_KEYS)

        if self._client is None:
            return False

        if needs_restart:
            await self.start(resume=self.session_id)
            return True

        try:
            if old.model != new.model:
                await self._client.set_model(new.model)
            if old.permission_mode != new.permission_mode:
                await self._client.set_permission_mode(new.permission_mode)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001 — на всякий случай поднимаем заново
            log.warning("Не удалось применить настройки на лету, перезапускаю", exc_info=True)
            await self.start(resume=self.session_id)
            return True
        return False

    async def context_usage(self) -> dict[str, Any] | None:
        if self._client is None:
            return None
        try:
            return dict(await self._client.get_context_usage())
        except Exception:  # noqa: BLE001
            log.debug("get_context_usage упал", exc_info=True)
            return None

    async def mcp_status(self) -> Any | None:
        if self._client is None:
            return None
        try:
            return await self._client.get_mcp_status()
        except Exception:  # noqa: BLE001
            log.debug("get_mcp_status упал", exc_info=True)
            return None


class SessionManager:
    """Реестр сессий: chat_key → ClaudeSession, плюс рабочие папки."""

    def __init__(
        self,
        workspace_root: Path,
        *,
        cli_path: str | None = None,
        turn_timeout: int = 3600,
    ) -> None:
        self.workspace_root = workspace_root
        self.cli_path = cli_path
        self.turn_timeout = turn_timeout
        self._sessions: dict[str, ClaudeSession] = {}
        self._guard = asyncio.Lock()

    def workspace_for(self, user_id: int, chat_key: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", chat_key)
        return self.workspace_root / str(user_id) / safe

    def peek(self, chat_key: str) -> ClaudeSession | None:
        return self._sessions.get(chat_key)

    async def get_or_create(
        self,
        chat_key: str,
        user_id: int,
        prefs: Prefs,
        *,
        workspace: Path | None = None,
        session_id: str | None = None,
        permission_cb: PermissionCallback | None = None,
    ) -> ClaudeSession:
        async with self._guard:
            existing = self._sessions.get(chat_key)
            if existing is not None:
                if not existing.connected:
                    await existing.start(resume=existing.session_id)
                return existing

            session = ClaudeSession(
                chat_key=chat_key,
                workspace=workspace or self.workspace_for(user_id, chat_key),
                prefs=prefs,
                cli_path=self.cli_path,
                permission_cb=permission_cb,
                session_id=session_id,
                turn_timeout=self.turn_timeout,
            )
            await session.start(resume=session_id)
            self._sessions[chat_key] = session
            return session

    async def drop(self, chat_key: str) -> None:
        async with self._guard:
            session = self._sessions.pop(chat_key, None)
        if session is not None:
            await session.close()

    async def close_all(self) -> None:
        async with self._guard:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            await session.close()

    def all(self) -> dict[str, ClaudeSession]:
        return dict(self._sessions)


def _preview_tool_result(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(str(item.get("text", "")))
                else:
                    parts.append(f"[{item.get('type', 'block')}]")
            else:
                parts.append(str(item))
        return "\n".join(p for p in parts if p)
    return str(content)
