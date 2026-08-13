"""Контроль доступа: пускаем только владельца и тех, кого он одобрил."""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass

from telegram import User

from .config import Settings
from .storage import STATUS_APPROVED, STATUS_BLOCKED, STATUS_PENDING, Storage, UserRecord

log = logging.getLogger(__name__)


@dataclass
class AccessDecision:
    allowed: bool
    status: str
    is_owner: bool = False
    just_requested: bool = False


class AccessControl:
    """Белый список: из .env (ALLOWED_USER_IDS) + одобренные владельцем в БД."""

    def __init__(self, settings: Settings, storage: Storage) -> None:
        self.settings = settings
        self.storage = storage

    def is_owner(self, user_id: int) -> bool:
        return user_id == self.settings.owner_id

    async def check(self, user: User) -> AccessDecision:
        """Проверить доступ. Незнакомца регистрируем как заявку (pending)."""
        if self.is_owner(user.id):
            return AccessDecision(allowed=True, status=STATUS_APPROVED, is_owner=True)

        if user.id in self.settings.allowed_user_ids:
            return AccessDecision(allowed=True, status=STATUS_APPROVED)

        record = await self.storage.get_user(user.id)
        if record and record.status == STATUS_APPROVED:
            return AccessDecision(allowed=True, status=STATUS_APPROVED)
        if record and record.status == STATUS_BLOCKED:
            return AccessDecision(allowed=False, status=STATUS_BLOCKED)
        if record and record.status == STATUS_PENDING:
            return AccessDecision(allowed=False, status=STATUS_PENDING)

        await self.storage.upsert_user(
            user.id, user.username, user.full_name, STATUS_PENDING
        )
        return AccessDecision(allowed=False, status=STATUS_PENDING, just_requested=True)

    async def approve(self, user_id: int) -> None:
        record = await self.storage.get_user(user_id)
        if record is None:
            await self.storage.upsert_user(user_id, None, None, STATUS_APPROVED)
        else:
            await self.storage.set_status(user_id, STATUS_APPROVED)

    async def block(self, user_id: int) -> None:
        record = await self.storage.get_user(user_id)
        if record is None:
            await self.storage.upsert_user(user_id, None, None, STATUS_BLOCKED)
        else:
            await self.storage.set_status(user_id, STATUS_BLOCKED)

    async def revoke(self, user_id: int) -> None:
        await self.storage.forget_user(user_id)

    async def approved_users(self) -> list[UserRecord]:
        return await self.storage.list_users(STATUS_APPROVED)

    async def pending_users(self) -> list[UserRecord]:
        return await self.storage.list_users(STATUS_PENDING)


def describe_user(user: User | UserRecord) -> str:
    """`Имя (@username, id)` — одинаково для telegram.User и записи из БД.

    Имя и username человек задаёт сам, а все места вывода — с parse_mode=HTML,
    поэтому экранируем здесь: иначе «Аня <3» роняет отправку целого сообщения.
    """
    if isinstance(user, UserRecord):
        name = user.full_name or "—"
        username = user.username
        uid = user.user_id
    else:
        name = user.full_name
        username = user.username
        uid = user.id
    handle = f"@{html.escape(username)}" if username else "без username"
    return f"{html.escape(name)} ({handle}, id {uid})"
