"""Каталог того, что можно переключать: модели, effort, режимы доступа, thinking.

Модели задаются **алиасами** там, где это возможно (`opus`, `sonnet`, `fable`,
`haiku`) — алиас всегда указывает на самую свежую модель линейки, поэтому меню
не устаревает при выходе новых версий. Конкретные версии оставлены отдельной
группой для тех случаев, когда нужно закрепиться на определённой модели.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Choice:
    """Один пункт меню настроек."""

    key: str
    label: str
    hint: str = ""

    @property
    def button(self) -> str:
        return self.label


# --- Модели -----------------------------------------------------------------
# Алиасы (рекомендуются): CLI сам разрешит их в актуальную версию.
MODEL_ALIASES: tuple[Choice, ...] = (
    Choice("opus", "🍎 Opus (актуальный)", "Флагман: сложный код, длинные агентные задачи"),
    Choice("sonnet", "🍏 Sonnet (актуальный)", "Баланс скорости и качества"),
    Choice("fable", "🍓 Fable (актуальный)", "Максимальная глубина рассуждений"),
    Choice("haiku", "🍡 Haiku (актуальный)", "Самый быстрый и дешёвый"),
)

# Явные версии — на случай, когда нужно закрепиться.
MODEL_PINNED: tuple[Choice, ...] = (
    Choice("claude-opus-5", "Opus 5", "Текущий Opus"),
    Choice("claude-opus-4-8", "Opus 4.8", "Предыдущий Opus"),
    Choice("claude-opus-4-7", "Opus 4.7", ""),
    Choice("claude-sonnet-5", "Sonnet 5", "Текущий Sonnet"),
    Choice("claude-sonnet-4-6", "Sonnet 4.6", ""),
    Choice("claude-fable-5", "Fable 5", ""),
    Choice("claude-haiku-4-5", "Haiku 4.5", ""),
)

MODELS: tuple[Choice, ...] = MODEL_ALIASES + MODEL_PINNED

# --- Effort (глубина рассуждений) ------------------------------------------
EFFORTS: tuple[Choice, ...] = (
    Choice("low", "🟢 Low", "Короткие задачи, минимум токенов и задержки"),
    Choice("medium", "🟡 Medium", "Экономный режим для рутины"),
    Choice("high", "🟠 High", "По умолчанию: баланс качества и расхода"),
    Choice("xhigh", "🔴 Xhigh", "Лучший выбор для кода и агентных задач"),
    Choice("max", "⚫️ Max", "Потолок: когда важнее точность, чем цена"),
)

# --- Режимы доступа к инструментам -----------------------------------------
PERMISSION_MODES: tuple[Choice, ...] = (
    Choice("default", "🛡 Спрашивать", "Каждое опасное действие — с кнопкой подтверждения"),
    Choice("acceptEdits", "✏️ Правки без спроса", "Файлы правит сам, остальное спрашивает"),
    Choice("plan", "🗺 План", "Только планирует, ничего не меняет"),
    Choice("dontAsk", "🤫 Не спрашивать", "Разрешено всё, что не запрещено правилами"),
    Choice("auto", "🎛 Авто", "Классификатор сам решает, когда спросить"),
    Choice("bypassPermissions", "⚠️ Без ограничений", "Полный обход проверок — только для песочницы"),
)

# --- Extended thinking ------------------------------------------------------
THINKING_MODES: tuple[Choice, ...] = (
    Choice("adaptive", "🧠 Адаптивное", "Модель сама решает, сколько думать"),
    Choice("summarized", "📖 С показом", "Адаптивное + краткая выжимка размышлений"),
    Choice("disabled", "💤 Выключено", "Без размышлений (недоступно на xhigh/max)"),
)

_BY_KEY: dict[str, dict[str, Choice]] = {
    "model": {c.key: c for c in MODELS},
    "effort": {c.key: c for c in EFFORTS},
    "mode": {c.key: c for c in PERMISSION_MODES},
    "thinking": {c.key: c for c in THINKING_MODES},
}


def get(kind: str, key: str) -> Choice | None:
    """Найти пункт каталога по типу и ключу."""
    return _BY_KEY.get(kind, {}).get(key)


def label(kind: str, key: str) -> str:
    """Человекочитаемое имя значения (или само значение, если его нет в каталоге)."""
    choice = get(kind, key)
    return choice.label if choice else key


def is_valid(kind: str, key: str) -> bool:
    if kind == "model":
        # Модели допускаем любые: CLI умеет и алиасы, и полные имена,
        # а новые версии выходят чаще, чем обновляется этот файл.
        return bool(key)
    return key in _BY_KEY.get(kind, {})


def thinking_config(mode: str, effort: str) -> dict[str, str] | None:
    """Собрать `thinking` для ClaudeAgentOptions.

    `disabled` доступно только при effort `high` и ниже — на `xhigh`/`max`
    API вернёт 400, поэтому здесь мягко откатываемся на адаптивное.
    """
    if mode == "disabled":
        if effort in {"xhigh", "max"}:
            return {"type": "adaptive", "display": "omitted"}
        return {"type": "disabled"}
    if mode == "summarized":
        return {"type": "adaptive", "display": "summarized"}
    return {"type": "adaptive", "display": "omitted"}


def model_emoji(model: str) -> str:
    lowered = model.lower()
    if "fable" in lowered:
        return "🍓"
    if "haiku" in lowered:
        return "🍡"
    if "sonnet" in lowered:
        return "🍏"
    return "🍎"
