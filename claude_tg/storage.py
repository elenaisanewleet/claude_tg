"""SQLite-хранилище: доступы, настройки, привязка чатов к сессиям Claude."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

STATUS_APPROVED = "approved"
STATUS_PENDING = "pending"
STATUS_BLOCKED = "blocked"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id     INTEGER PRIMARY KEY,
    username    TEXT,
    full_name   TEXT,
    status      TEXT NOT NULL,
    requested_at INTEGER,
    decided_at  INTEGER,
    note        TEXT
);

CREATE TABLE IF NOT EXISTS prefs (
    scope   TEXT PRIMARY KEY,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_sessions (
    chat_key   TEXT PRIMARY KEY,
    session_id TEXT,
    workspace  TEXT NOT NULL,
    title      TEXT,
    user_id    INTEGER,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@dataclass
class UserRecord:
    user_id: int
    username: str | None
    full_name: str | None
    status: str
    requested_at: int | None = None
    decided_at: int | None = None
    note: str | None = None


@dataclass
class Prefs:
    """Настройки одного чата/треда. Значения по умолчанию задаёт Settings."""

    model: str
    effort: str
    permission_mode: str
    thinking: str
    show_thinking: bool = True
    show_tools: bool = True
    show_footer: bool = True
    allowed_tools: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(self.__dict__, ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str, fallback: Prefs) -> Prefs:
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return fallback
        merged = {**fallback.__dict__, **{k: v for k, v in data.items() if k in fallback.__dict__}}
        return cls(**merged)


@dataclass
class ChatSession:
    chat_key: str
    session_id: str | None
    workspace: str
    title: str | None
    user_id: int | None
    updated_at: int


class Storage:
    """Тонкая обёртка над sqlite3. Все операции — под общим asyncio-локом."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        self._conn: sqlite3.Connection | None = None

    async def connect(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.executescript(_SCHEMA)
        conn.commit()
        self._conn = conn

    async def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Storage.connect() не вызван")
        return self._conn

    async def _run(self, fn, *args):
        async with self._lock:
            return await asyncio.to_thread(fn, *args)

    # --- пользователи -------------------------------------------------------

    async def upsert_user(
        self,
        user_id: int,
        username: str | None,
        full_name: str | None,
        status: str,
        note: str | None = None,
    ) -> None:
        def _do() -> None:
            now = int(time.time())
            self.conn.execute(
                """
                INSERT INTO users (user_id, username, full_name, status, requested_at, decided_at, note)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    full_name = excluded.full_name,
                    status = excluded.status,
                    decided_at = excluded.decided_at,
                    note = COALESCE(excluded.note, users.note)
                """,
                (
                    user_id,
                    username,
                    full_name,
                    status,
                    now,
                    now if status != STATUS_PENDING else None,
                    note,
                ),
            )
            self.conn.commit()

        await self._run(_do)

    async def get_user(self, user_id: int) -> UserRecord | None:
        def _do() -> UserRecord | None:
            row = self.conn.execute(
                "SELECT * FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
            return _row_to_user(row) if row else None

        return await self._run(_do)

    async def list_users(self, status: str | None = None) -> list[UserRecord]:
        def _do() -> list[UserRecord]:
            if status:
                rows = self.conn.execute(
                    "SELECT * FROM users WHERE status = ? ORDER BY user_id", (status,)
                ).fetchall()
            else:
                rows = self.conn.execute("SELECT * FROM users ORDER BY status, user_id").fetchall()
            return [_row_to_user(r) for r in rows]

        return await self._run(_do)

    async def set_status(self, user_id: int, status: str) -> None:
        def _do() -> None:
            self.conn.execute(
                "UPDATE users SET status = ?, decided_at = ? WHERE user_id = ?",
                (status, int(time.time()), user_id),
            )
            self.conn.commit()

        await self._run(_do)

    async def forget_user(self, user_id: int) -> None:
        def _do() -> None:
            self.conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
            self.conn.commit()

        await self._run(_do)

    # --- настройки ----------------------------------------------------------

    async def get_prefs(self, scope: str, fallback: Prefs) -> Prefs:
        def _do() -> Prefs:
            row = self.conn.execute("SELECT payload FROM prefs WHERE scope = ?", (scope,)).fetchone()
            if not row:
                return Prefs(**fallback.__dict__)
            return Prefs.from_json(row["payload"], fallback)

        return await self._run(_do)

    async def save_prefs(self, scope: str, prefs: Prefs) -> None:
        def _do() -> None:
            self.conn.execute(
                "INSERT INTO prefs (scope, payload) VALUES (?, ?) "
                "ON CONFLICT(scope) DO UPDATE SET payload = excluded.payload",
                (scope, prefs.to_json()),
            )
            self.conn.commit()

        await self._run(_do)

    # --- сессии -------------------------------------------------------------

    async def get_chat_session(self, chat_key: str) -> ChatSession | None:
        def _do() -> ChatSession | None:
            row = self.conn.execute(
                "SELECT * FROM chat_sessions WHERE chat_key = ?", (chat_key,)
            ).fetchone()
            if not row:
                return None
            return ChatSession(
                chat_key=row["chat_key"],
                session_id=row["session_id"],
                workspace=row["workspace"],
                title=row["title"],
                user_id=row["user_id"],
                updated_at=row["updated_at"],
            )

        return await self._run(_do)

    async def save_chat_session(
        self,
        chat_key: str,
        workspace: str,
        session_id: str | None,
        user_id: int | None,
        title: str | None = None,
    ) -> None:
        def _do() -> None:
            self.conn.execute(
                """
                INSERT INTO chat_sessions (chat_key, session_id, workspace, title, user_id, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_key) DO UPDATE SET
                    session_id = excluded.session_id,
                    workspace  = excluded.workspace,
                    title      = COALESCE(excluded.title, chat_sessions.title),
                    user_id    = excluded.user_id,
                    updated_at = excluded.updated_at
                """,
                (chat_key, session_id, workspace, title, user_id, int(time.time())),
            )
            self.conn.commit()

        await self._run(_do)

    async def clear_chat_session(self, chat_key: str) -> None:
        def _do() -> None:
            self.conn.execute("DELETE FROM chat_sessions WHERE chat_key = ?", (chat_key,))
            self.conn.commit()

        await self._run(_do)

    # --- служебный key-value ------------------------------------------------

    async def get_kv(self, key: str) -> str | None:
        def _do() -> str | None:
            row = self.conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else None

        return await self._run(_do)

    async def set_kv(self, key: str, value: str) -> None:
        def _do() -> None:
            self.conn.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            self.conn.commit()

        await self._run(_do)


def _row_to_user(row: Any) -> UserRecord:
    return UserRecord(
        user_id=row["user_id"],
        username=row["username"],
        full_name=row["full_name"],
        status=row["status"],
        requested_at=row["requested_at"],
        decided_at=row["decided_at"],
        note=row["note"],
    )
