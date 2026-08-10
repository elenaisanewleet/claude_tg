"""Конфигурация claude-tg: всё читается из окружения (.env)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:  # python-dotenv не обязателен, но с ним удобнее
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None  # type: ignore[assignment]


class ConfigError(RuntimeError):
    """Не хватает обязательной настройки."""


def _parse_ids(raw: str | None) -> frozenset[int]:
    if not raw:
        return frozenset()
    ids: set[int] = set()
    for chunk in raw.replace(";", ",").replace(" ", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            ids.add(int(chunk))
        except ValueError as exc:
            raise ConfigError(f"Некорректный user_id в списке доступа: {chunk!r}") from exc
    return frozenset(ids)


def _bool(raw: str | None, default: bool) -> bool:
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on", "да"}


@dataclass(frozen=True)
class Settings:
    """Полный набор настроек бота."""

    bot_token: str
    owner_id: int
    allowed_user_ids: frozenset[int]

    data_dir: Path
    workspace_root: Path
    db_path: Path

    claude_cli: str | None
    default_model: str
    default_effort: str
    default_permission_mode: str
    default_thinking: str
    default_show_thinking: bool
    default_show_tools: bool
    default_show_footer: bool

    stream_interval: float
    message_limit: int
    max_download_mb: int
    permission_timeout: int
    turn_timeout: int

    auto_update: bool
    auto_update_hour: int

    log_level: str

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        if load_dotenv is not None and env is None:
            load_dotenv(override=False)
        src = dict(os.environ) if env is None else dict(env)

        token = (src.get("TELEGRAM_BOT_TOKEN") or "").strip()
        if not token:
            raise ConfigError(
                "Не задан TELEGRAM_BOT_TOKEN. Возьми токен у @BotFather и положи в .env"
            )

        owner_raw = (src.get("OWNER_USER_ID") or "").strip()
        if not owner_raw:
            raise ConfigError(
                "Не задан OWNER_USER_ID — твой Telegram user_id. "
                "Узнать можно у @userinfobot или командой /id в любом боте."
            )
        try:
            owner_id = int(owner_raw)
        except ValueError as exc:
            raise ConfigError(f"OWNER_USER_ID должен быть числом, получено {owner_raw!r}") from exc

        data_dir = Path(src.get("DATA_DIR") or (Path.home() / ".claude-tg")).expanduser()
        workspace_root = Path(
            src.get("WORKSPACE_ROOT") or (data_dir / "workspaces")
        ).expanduser()
        db_path = Path(src.get("DB_PATH") or (data_dir / "claude-tg.sqlite3")).expanduser()

        allowed = set(_parse_ids(src.get("ALLOWED_USER_IDS")))
        allowed.add(owner_id)

        return cls(
            bot_token=token,
            owner_id=owner_id,
            allowed_user_ids=frozenset(allowed),
            data_dir=data_dir,
            workspace_root=workspace_root,
            db_path=db_path,
            claude_cli=(src.get("CLAUDE_CLI_PATH") or "").strip() or None,
            default_model=(src.get("DEFAULT_MODEL") or "opus").strip(),
            default_effort=(src.get("DEFAULT_EFFORT") or "high").strip(),
            default_permission_mode=(src.get("DEFAULT_PERMISSION_MODE") or "default").strip(),
            default_thinking=(src.get("DEFAULT_THINKING") or "adaptive").strip(),
            default_show_thinking=_bool(src.get("SHOW_THINKING"), True),
            default_show_tools=_bool(src.get("SHOW_TOOLS"), True),
            default_show_footer=_bool(src.get("SHOW_FOOTER"), True),
            stream_interval=float(src.get("STREAM_INTERVAL") or 1.4),
            message_limit=int(src.get("MESSAGE_LIMIT") or 3500),
            max_download_mb=int(src.get("MAX_DOWNLOAD_MB") or 20),
            permission_timeout=int(src.get("PERMISSION_TIMEOUT") or 300),
            turn_timeout=int(src.get("TURN_TIMEOUT") or 3600),
            auto_update=_bool(src.get("AUTO_UPDATE"), True),
            auto_update_hour=int(src.get("AUTO_UPDATE_HOUR") or 5),
            log_level=(src.get("LOG_LEVEL") or "INFO").strip().upper(),
        )

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
