"""Общий контекст приложения: настройки, БД, доступы, сессии, разрешения."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny, ToolPermissionContext
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from .access import AccessControl
from .bridge import ClaudeSession, SessionManager
from .config import Settings
from .storage import Prefs, Storage
from .streamer import describe_tool, tool_icon
from .telegram_md import truncate
from .ui import permission_menu

log = logging.getLogger(__name__)

BOT_DATA_KEY = "claude_tg_app"


@dataclass
class Quota:
    """Сколько человек израсходовал в скользящем окне и сколько ему можно."""

    limit: float | None  # None — без ограничений
    spent: float
    window_hours: int
    frees_up_in: int | None = None  # секунд до выхода самого раннего хода из окна

    @property
    def unlimited(self) -> bool:
        return self.limit is None

    @property
    def allowed(self) -> bool:
        return self.unlimited or self.spent < self.limit

    @property
    def left(self) -> float:
        return 0.0 if self.unlimited else max(0.0, self.limit - self.spent)

    @property
    def fraction(self) -> float:
        if self.unlimited or not self.limit:
            return 0.0
        return min(1.0, self.spent / self.limit)

    @property
    def period(self) -> str:
        days = self.window_hours // 24
        return f"{days} дн." if days else f"{self.window_hours} ч."

    def describe(self) -> str:
        if self.unlimited:
            return f"без ограничений · израсходовано ${self.spent:.2f} за {self.period}"
        return (
            f"${self.spent:.2f} из ${self.limit:.2f} за {self.period} "
            f"({self.fraction:.0%}), осталось ${self.left:.2f}"
        )

    def short(self) -> str:
        """Совсем коротко — помещается на кнопку рядом с именем."""
        if self.unlimited:
            return f"${self.spent:.2f} · без лимита"
        return f"${self.spent:.2f} из ${self.limit:.2f}"

    def refusal(self) -> str:
        text = (
            f"🚦 Лимит исчерпан: израсходовано ${self.spent:.2f} "
            f"из ${self.limit:.2f} за {self.period}."
        )
        if self.frees_up_in:
            hours = self.frees_up_in // 3600
            if hours >= 24:
                text += f"\nЧасть лимита освободится примерно через {hours // 24} дн."
            elif hours >= 1:
                text += f"\nЧасть лимита освободится примерно через {hours} ч."
            else:
                text += "\nЧасть лимита освободится в течение часа."
        text += "\n\nЕсли нужно больше — попроси владельца бота поднять лимит."
        return text


@dataclass
class PendingPermission:
    future: asyncio.Future
    tool_name: str
    chat_id: int
    message_id: int | None = None


@dataclass
class AppContext:
    settings: Settings
    storage: Storage
    access: AccessControl
    sessions: SessionManager
    bot: Any = None
    targets: dict[str, tuple[int, int | None]] = field(default_factory=dict)
    pending: dict[str, PendingPermission] = field(default_factory=dict)

    # --- настройки ----------------------------------------------------------

    def default_prefs(self) -> Prefs:
        s = self.settings
        return Prefs(
            model=s.default_model,
            effort=s.default_effort,
            permission_mode=s.default_permission_mode,
            thinking=s.default_thinking,
            show_thinking=s.default_show_thinking,
            show_tools=s.default_show_tools,
            show_footer=s.default_show_footer,
            allowed_tools=[],
        )

    async def prefs_for(self, chat_key: str) -> Prefs:
        return await self.storage.get_prefs(chat_key, self.default_prefs())

    async def save_prefs(self, chat_key: str, prefs: Prefs) -> None:
        await self.storage.save_prefs(chat_key, prefs)
        session = self.sessions.peek(chat_key)
        if session is not None:
            await session.apply_prefs(prefs)

    # --- сессии -------------------------------------------------------------

    async def session_for(self, chat_key: str, user_id: int) -> ClaudeSession:
        prefs = await self.prefs_for(chat_key)
        stored = await self.storage.get_chat_session(chat_key)
        workspace = Path(stored.workspace) if stored else self.sessions.workspace_for(user_id, chat_key)
        session = await self.sessions.get_or_create(
            chat_key,
            user_id,
            prefs,
            workspace=workspace,
            session_id=stored.session_id if stored else None,
            permission_cb=self.request_permission,
        )
        await self.storage.save_chat_session(
            chat_key, str(session.workspace), session.session_id, user_id
        )
        return session

    async def remember_session(self, chat_key: str, session: ClaudeSession, user_id: int) -> None:
        await self.storage.save_chat_session(
            chat_key, str(session.workspace), session.session_id, user_id
        )

    def set_target(self, chat_key: str, chat_id: int, thread_id: int | None) -> None:
        self.targets[chat_key] = (chat_id, thread_id)

    # --- лимиты расхода -----------------------------------------------------

    def window_start(self) -> int:
        return int(time.time()) - self.settings.budget_window_hours * 3600

    async def spenders(self) -> set[int]:
        """Все, у кого есть расход в окне, — включая потерявших доступ."""
        return set(await self.storage.spending_by_user(self.window_start()))

    async def quota_for(self, user_id: int) -> Quota:
        """Сколько человеку осталось. Владелец по умолчанию без ограничений."""
        explicit, budget = await self.storage.get_limit(user_id)
        if not explicit:
            budget = None if self.access.is_owner(user_id) else self.settings.default_budget_usd

        since = self.window_start()
        spent = await self.storage.spent_since(user_id, since)
        frees_up_in: int | None = None
        if budget is not None and spent >= budget:
            oldest = await self.storage.oldest_usage_at(user_id, since)
            if oldest is not None:
                window = self.settings.budget_window_hours * 3600
                frees_up_in = max(0, oldest + window - int(time.time()))

        return Quota(
            limit=budget,
            spent=spent,
            window_hours=self.settings.budget_window_hours,
            frees_up_in=frees_up_in,
        )

    async def record_turn(self, user_id: int, cost_usd: float, model: str | None) -> None:
        if cost_usd <= 0:
            return
        await self.storage.record_usage(user_id, cost_usd, model)

    async def set_budget(self, user_id: int, budget_usd: float | None) -> None:
        await self.storage.set_limit(user_id, budget_usd)

    async def reset_budget(self, user_id: int) -> None:
        await self.storage.clear_limit(user_id)

    # --- разрешения инструментов -------------------------------------------

    async def request_permission(
        self,
        session: ClaudeSession,
        tool_name: str,
        tool_input: dict[str, Any],
        context: ToolPermissionContext,
    ) -> PermissionResultAllow | PermissionResultDeny:
        target = self.targets.get(session.chat_key)
        if target is None or self.bot is None:
            log.warning("Некуда спросить разрешение для %s, разрешаю по умолчанию", tool_name)
            return PermissionResultAllow(updated_input=tool_input)

        chat_id, thread_id = target
        request_id = uuid.uuid4().hex[:12]
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self.pending[request_id] = PendingPermission(future, tool_name, chat_id)

        title = context.title or context.display_name or tool_name
        detail = context.description or describe_tool(tool_name, tool_input)
        text = (
            f"{tool_icon(tool_name)} <b>Разрешить {title}?</b>\n"
            f"<code>{truncate(detail, 500)}</code>"
        )
        if context.blocked_path:
            text += f"\n\nПуть: <code>{truncate(context.blocked_path, 200)}</code>"

        kwargs: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": ParseMode.HTML,
            "reply_markup": permission_menu(request_id),
        }
        if thread_id is not None:
            kwargs["message_thread_id"] = thread_id
        try:
            message = await self.bot.send_message(**kwargs)
            self.pending[request_id].message_id = message.message_id
        except Exception:  # noqa: BLE001 — не смогли спросить → безопасно отказываем
            log.exception("Не удалось отправить запрос разрешения")
            self.pending.pop(request_id, None)
            return PermissionResultDeny(message="Не смог спросить пользователя", interrupt=False)

        try:
            decision = await asyncio.wait_for(future, timeout=self.settings.permission_timeout)
        except TimeoutError:
            decision = "timeout"
        finally:
            self.pending.pop(request_id, None)

        if decision == "always":
            session.session_allow.add(tool_name)
            return PermissionResultAllow(updated_input=tool_input)
        if decision == "allow":
            return PermissionResultAllow(updated_input=tool_input)
        if decision == "timeout":
            return PermissionResultDeny(
                message="Пользователь не ответил вовремя — действие отменено", interrupt=False
            )
        return PermissionResultDeny(message="Пользователь запретил это действие", interrupt=False)

    def resolve_permission(self, request_id: str, decision: str) -> PendingPermission | None:
        pending = self.pending.get(request_id)
        if pending is None or pending.future.done():
            return None
        pending.future.set_result(decision)
        return pending


def get_app(context: ContextTypes.DEFAULT_TYPE) -> AppContext:
    app: AppContext = context.application.bot_data[BOT_DATA_KEY]
    return app


def chat_key_for(update: Update) -> str:
    """Уникальный ключ чата/треда — одна сессия Claude на один такой ключ."""
    chat = update.effective_chat
    message = update.effective_message
    thread_id = getattr(message, "message_thread_id", None) if message else None
    if chat is None:
        user = update.effective_user
        return f"u{user.id if user else 0}"
    if chat.type == "private":
        return f"p{chat.id}"
    if thread_id:
        return f"g{chat.id}:{thread_id}"
    return f"g{chat.id}"


def thread_id_for(update: Update) -> int | None:
    message = update.effective_message
    if message is None:
        return None
    chat = update.effective_chat
    if chat is not None and chat.type == "private":
        return None
    return getattr(message, "message_thread_id", None)
