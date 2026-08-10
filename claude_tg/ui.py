"""Клавиатуры и разметка callback_data.

Формат callback_data (лимит Telegram — 64 байта):
    menu:<screen>                 — переход по меню настроек
    set:<kind>:<key>              — выбрать значение (model/effort/mode/thinking)
    tgl:<flag>                    — переключить булев флаг
    perm:<request_id>:<decision>  — ответ на запрос разрешения инструмента
    acc:<user_id>:<decision>      — решение владельца по заявке на доступ
    sess:<action>                 — действия с сессией
"""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from . import catalog
from .storage import Prefs


def _rows(buttons: list[InlineKeyboardButton], per_row: int) -> list[list[InlineKeyboardButton]]:
    return [buttons[i : i + per_row] for i in range(0, len(buttons), per_row)]


def _mark(active: bool, label: str) -> str:
    return f"✓ {label}" if active else label


def main_menu(prefs: Prefs) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                f"🧠 Модель · {prefs.model}", callback_data="menu:model"
            )
        ],
        [
            InlineKeyboardButton(
                f"⚡ Effort · {prefs.effort}", callback_data="menu:effort"
            ),
            InlineKeyboardButton(
                f"🎛 Режим · {catalog.label('mode', prefs.permission_mode).split(' ', 1)[-1]}",
                callback_data="menu:mode",
            ),
        ],
        [
            InlineKeyboardButton(
                f"💭 Thinking · {catalog.label('thinking', prefs.thinking).split(' ', 1)[-1]}",
                callback_data="menu:thinking",
            )
        ],
        [InlineKeyboardButton("👁 Что показывать", callback_data="menu:view")],
        [InlineKeyboardButton("📊 Статус сессии", callback_data="menu:status")],
        [InlineKeyboardButton("✖️ Закрыть", callback_data="menu:close")],
    ]
    return InlineKeyboardMarkup(rows)


def model_menu(prefs: Prefs) -> InlineKeyboardMarkup:
    alias_buttons = [
        InlineKeyboardButton(_mark(prefs.model == c.key, c.label), callback_data=f"set:model:{c.key}")
        for c in catalog.MODEL_ALIASES
    ]
    pinned_buttons = [
        InlineKeyboardButton(_mark(prefs.model == c.key, c.label), callback_data=f"set:model:{c.key}")
        for c in catalog.MODEL_PINNED
    ]
    rows = _rows(alias_buttons, 2)
    rows.append([InlineKeyboardButton("— конкретные версии —", callback_data="menu:noop")])
    rows += _rows(pinned_buttons, 2)
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="menu:main")])
    return InlineKeyboardMarkup(rows)


def choice_menu(kind: str, choices, active: str) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(_mark(active == c.key, c.label), callback_data=f"set:{kind}:{c.key}")
        for c in choices
    ]
    rows = _rows(buttons, 2)
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="menu:main")])
    return InlineKeyboardMarkup(rows)


def view_menu(prefs: Prefs) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                f"{'✅' if prefs.show_thinking else '⬜️'} Размышления",
                callback_data="tgl:show_thinking",
            )
        ],
        [
            InlineKeyboardButton(
                f"{'✅' if prefs.show_tools else '⬜️'} Вызовы инструментов",
                callback_data="tgl:show_tools",
            )
        ],
        [
            InlineKeyboardButton(
                f"{'✅' if prefs.show_footer else '⬜️'} Футер (модель, цена, время)",
                callback_data="tgl:show_footer",
            )
        ],
        [InlineKeyboardButton("⬅️ Назад", callback_data="menu:main")],
    ]
    return InlineKeyboardMarkup(rows)


def permission_menu(request_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Разрешить", callback_data=f"perm:{request_id}:allow"),
                InlineKeyboardButton("⛔️ Запретить", callback_data=f"perm:{request_id}:deny"),
            ],
            [
                InlineKeyboardButton(
                    "♾ Всегда в этой сессии", callback_data=f"perm:{request_id}:always"
                )
            ],
        ]
    )


def access_menu(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Открыть доступ", callback_data=f"acc:{user_id}:approve"),
                InlineKeyboardButton("⛔️ Отказать", callback_data=f"acc:{user_id}:deny"),
            ]
        ]
    )


def sessions_menu(has_session: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🆕 Новая сессия", callback_data="sess:new")],
    ]
    if has_session:
        rows.append(
            [
                InlineKeyboardButton("🌿 Ответвить", callback_data="sess:fork"),
                InlineKeyboardButton("🔁 Перезапустить", callback_data="sess:restart"),
            ]
        )
    rows.append([InlineKeyboardButton("📚 Список сессий", callback_data="sess:list")])
    rows.append([InlineKeyboardButton("✖️ Закрыть", callback_data="menu:close")])
    return InlineKeyboardMarkup(rows)


def stop_button() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⏹ Остановить", callback_data="sess:stop")]])
