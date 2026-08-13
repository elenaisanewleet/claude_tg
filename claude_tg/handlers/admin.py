"""Команды владельца и базовые: /start, /help, /id, доступы, обновления."""

from __future__ import annotations

import asyncio
import html
import logging
import re
import time

from telegram import InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from .. import ui, updater
from ..access import describe_user, user_title
from ..app import get_app
from .common import guarded, owner_only, reply

log = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")

RESTART_DELAY = 1.5  # даём ответу уйти до остановки процесса
PROGRESS_INTERVAL = 2.0  # не чаще одной правки сообщения в 2 секунды
PROGRESS_WIDTH = 12
LINE_LIMIT = 120


def progress_text(event: updater.ProgressEvent) -> str:
    """Шкала, текущий шаг и последняя строка вывода команды."""
    filled = round(event.fraction * PROGRESS_WIDTH)
    bar = "▰" * filled + "▱" * (PROGRESS_WIDTH - filled)
    percent = round(event.fraction * 100)
    elapsed = f"{int(event.elapsed) // 60:d}:{int(event.elapsed) % 60:02d}"

    if event.index > event.total:
        head = f"✅ Готово · {elapsed}"
    else:
        head = f"🔄 Шаг {event.index} из {event.total} · {elapsed}"

    lines = [head, f"{bar} {percent}%", f"<code>{html.escape(event.label)}</code>"]
    if event.line:
        lines.append(f"<code>{html.escape(event.line[:LINE_LIMIT])}</code>")
    return "\n".join(lines)

HELP = """🤖 <b>Claude в Telegram</b>

Просто пиши — я передам сообщение Claude Code и покажу ответ по мере генерации.
Файлы, фото и документы можно кидать прямо в чат: они попадают в рабочую папку.

<b>Диалог</b>
/new — начать новую сессию
/sessions — список сессий, вернуться к любой
/resume &lt;id&gt; — продолжить конкретную сессию
/fork — ответвить копию текущей
/stop — прервать текущий ход
/status — модель, режим, расход контекста
/ws — рабочая папка и её содержимое

<b>Настройки</b>
/settings — общее меню
/model [алиас|имя] — модель (<code>opus</code>, <code>sonnet</code>, <code>fable</code>, <code>haiku</code>)
/effort [low|medium|high|xhigh|max] — глубина рассуждений
/mode [default|acceptEdits|plan|dontAsk|auto|bypassPermissions] — режим доступа
/thinking [adaptive|summarized|disabled] — размышления

<b>Сервис</b>
/version — версии Claude Code, SDK и бота
/help — эта справка
"""

OWNER_HELP = """

<b>Только для владельца</b>
/users — кто имеет доступ и кто ждёт решения
/grant &lt;user_id&gt; — открыть доступ
/revoke &lt;user_id&gt; — закрыть доступ
/block &lt;user_id&gt; — заблокировать молча
/update — обновить Claude Code и SDK прямо сейчас
/restart — перезапустить бота (подхватить обновление)
/limits — расход и лимиты; там же кнопки, чтобы поменять потолок
/limit &lt;user_id&gt; &lt;сумма|нет|сброс&gt; — то же самое текстом
"""


@guarded
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = get_app(context)
    user = update.effective_user
    text = HELP
    if user is not None and app.access.is_owner(user.id):
        text += OWNER_HELP
    await reply(update, text)


@guarded
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, context)


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Доступно всем: без user_id владелец не сможет открыть доступ."""
    user = update.effective_user
    chat = update.effective_chat
    if user is None or chat is None:
        return
    await reply(
        update,
        f"🆔 Твой user_id: <code>{user.id}</code>\nЧат: <code>{chat.id}</code>",
    )


@guarded
async def cmd_version(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = get_app(context)
    installed = await updater.cli_version(app.settings.claude_cli)
    latest = await updater.latest_cli_version()
    fresh = "✅ актуальна" if latest and latest in installed else ("🔄 есть новее" if latest else "")
    lines = [
        "<b>Версии</b>",
        f"Claude Code: <code>{installed or 'не найден'}</code> {fresh}",
    ]
    if latest:
        lines.append(f"В npm сейчас: <code>{latest}</code>")
    lines.append(f"claude-agent-sdk: <code>{updater.sdk_version() or '?'}</code>")
    lines.append(f"claude-tg: <code>{updater.bot_version()}</code>")
    await reply(update, "\n".join(lines))


@owner_only
async def cmd_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = get_app(context)
    message = await reply(update, "🔄 Обновляю Claude Code и SDK…")
    last_edit = 0.0

    async def show_step(event: updater.ProgressEvent) -> None:
        """Шкала и последняя строка вывода: brew качает пакет минутами.

        Правки прорежаем — Telegram не любит частых edit'ов, а вывод команд
        сыплется десятками строк в секунду.
        """
        nonlocal last_edit
        if message is None:
            return
        now = time.monotonic()
        final = event.index > event.total
        if not final and now - last_edit < PROGRESS_INTERVAL:
            return
        last_edit = now
        await message.edit_text(progress_text(event), parse_mode=ParseMode.HTML)

    try:
        report = await updater.run_update(app.settings.claude_cli, progress=show_step)
        text = report.to_text()
    except Exception as exc:  # noqa: BLE001 — отчёт об ошибке полезнее молчания
        log.exception("Обновление не удалось")
        text = f"⚠️ Обновление сорвалось: <code>{html.escape(str(exc))[:400]}</code>"
    await _deliver(update, message, text)


async def _deliver(update: Update, message, text: str) -> None:
    """Показать отчёт, не потеряв его из-за разметки.

    Вывод команд может содержать что угодно, поэтому если Telegram не принял
    HTML — отправляем то же самое без тегов, а не «что-то пошло не так».
    """
    plain = _TAG_RE.sub("", text)
    attempts = []
    if message is not None:
        attempts.append(lambda: message.edit_text(text, parse_mode=ParseMode.HTML))
        attempts.append(lambda: message.edit_text(plain))
    attempts.append(lambda: reply(update, text))
    attempts.append(lambda: reply(update, plain, parse_mode=None))

    for attempt in attempts:
        try:
            await attempt()
            return
        except Exception:  # noqa: BLE001 — пробуем следующий способ
            continue
    log.error("Не смог доставить отчёт об обновлении: %s", plain[:300])


@owner_only
async def cmd_restart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Перезапустить процесс — чтобы подхватить обновление, не подходя к маку.

    Поднимает обратно супервизор: launchd с KeepAlive или systemd с
    Restart=always. Запущенный вручную бот после этого просто остановится.
    """
    await reply(
        update,
        "♻️ Перезапускаюсь — вернусь через пару секунд.\n"
        "Если бот запущен вручную из терминала, подними его сам: "
        "<code>./.venv/bin/python -m claude_tg</code>",
    )
    context.application.create_task(_stop_soon(context.application))


async def _stop_soon(application) -> None:
    # Пауза, чтобы ответ успел уйти до закрытия соединения.
    await asyncio.sleep(RESTART_DELAY)
    log.info("Перезапуск по команде владельца")
    application.stop_running()


@owner_only
async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = get_app(context)
    approved = await app.access.approved_users()
    pending = await app.access.pending_users()

    lines = ["👥 <b>Доступ к боту</b>", ""]
    lines.append(f"Владелец: <code>{app.settings.owner_id}</code>")
    env_ids = sorted(app.settings.allowed_user_ids - {app.settings.owner_id})
    if env_ids:
        lines.append("Из .env: " + ", ".join(f"<code>{i}</code>" for i in env_ids))
    lines.append("")
    lines.append("<b>Одобрены</b>: " + (", ".join(describe_user(u) for u in approved) or "никого"))
    if pending:
        lines.append("")
        lines.append("<b>Ждут решения</b>:")
        for user in pending:
            lines.append(f"• {describe_user(user)} — /grant {user.user_id}")
    await reply(update, "\n".join(lines))


async def limits_screen(app) -> tuple[str, InlineKeyboardMarkup]:
    """Общий экран: список людей текстом и те же люди кнопками."""
    lines = ["💰 <b>Расход и лимиты</b>", ""]
    entries: list[tuple[int, str, str]] = []

    for user_id, title, label in await _roster(app):
        quota = await app.quota_for(user_id)
        mark = "♾" if quota.unlimited else ("🚫" if not quota.allowed else "✅")
        lines.append(f"{mark} {title}\n   {quota.describe()}")
        entries.append((user_id, f"{mark} {label}", quota.short()))

    default = app.settings.default_budget_usd
    window = app.settings.budget_window_hours // 24
    lines.append("")
    lines.append(f"По умолчанию новым: ${default:.2f} за {window} дн.")
    lines.append("Нажми на человека, чтобы поменять ему потолок.")
    return "\n".join(lines), ui.limits_menu(entries)


async def limit_card_screen(app, user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Карточка одного человека с кнопками сумм."""
    quota = await app.quota_for(user_id)
    title = next(
        (t for uid, t, _ in await _roster(app) if uid == user_id),
        f"<code>{user_id}</code>",
    )
    text = "\n".join(
        [
            "💰 <b>Лимит расхода</b>",
            "",
            title,
            quota.describe(),
            "",
            "Выбери потолок или сними ограничение совсем.",
        ]
    )
    return text, ui.limit_card(user_id, quota.unlimited, quota.limit)


@owner_only
async def cmd_limits(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кто сколько израсходовал за окно и какой у кого потолок."""
    text, markup = await limits_screen(get_app(context))
    await reply(update, text, reply_markup=markup)


async def on_limit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопки под /limits: выбрать человека и поменять ему потолок."""
    query = update.callback_query
    app = get_app(context)
    if query is None or not query.data:
        return
    actor = update.effective_user
    if actor is None or not app.access.is_owner(actor.id):
        await query.answer("Только владелец может менять лимиты", show_alert=True)
        return

    parts = query.data.split(":")
    action = parts[1]

    if action == "list":
        await query.answer()
        text, markup = await limits_screen(app)
        await _edit(query, text, markup)
        return

    user_id = int(parts[2])
    note: str | None = None

    if action == "set":
        budget = float(parts[3])
        await app.set_budget(user_id, budget)
        await query.answer(f"Лимит ${budget:.0f}")
        note = f"💰 Владелец обновил твой лимит: ${budget:.2f}"
    elif action == "off":
        await app.set_budget(user_id, None)
        await query.answer("Без ограничений")
        note = "♾ Владелец снял ограничение расхода"
    elif action == "def":
        await app.reset_budget(user_id)
        await query.answer("Вернул умолчание")
        note = f"💰 Владелец вернул лимит по умолчанию: ${app.settings.default_budget_usd:.2f}"
    else:
        await query.answer()

    text, markup = await limit_card_screen(app, user_id)
    await _edit(query, text, markup)
    if note and not app.access.is_owner(user_id):
        await _notify_user(context, user_id, note)


@owner_only
async def cmd_limit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/limit <user_id> <сумма|нет|сброс>` — потолок расхода за окно."""
    app = get_app(context)
    if len(context.args or []) < 2:
        await cmd_limits(update, context)
        return

    user_id = _parse_user_id(context)
    if user_id is None:
        await reply(update, "Первым аргументом нужен user_id: <code>/limit 12345 20</code>")
        return

    raw = context.args[1].strip().lower().replace(",", ".").lstrip("$")
    if raw in {"нет", "no", "off", "без", "unlimited", "∞"}:
        await app.set_budget(user_id, None)
        await reply(update, f"♾ Снял ограничение для <code>{user_id}</code>")
        return
    if raw in {"сброс", "reset", "default", "умолчание"}:
        await app.reset_budget(user_id)
        quota = await app.quota_for(user_id)
        await reply(update, f"↩️ Вернул умолчание для <code>{user_id}</code>: {quota.describe()}")
        return

    try:
        budget = float(raw)
    except ValueError:
        await reply(update, f"Не понял сумму: <code>{context.args[1]}</code>")
        return
    if budget < 0:
        await reply(update, "Сумма не может быть отрицательной.")
        return

    await app.set_budget(user_id, budget)
    quota = await app.quota_for(user_id)
    await reply(update, f"💰 Лимит для <code>{user_id}</code>: {quota.describe()}")
    if not app.access.is_owner(user_id):
        await _notify_user(
            context,
            user_id,
            f"💰 Владелец обновил твой лимит: ${budget:.2f} за {quota.period}",
        )


async def _roster(app) -> list[tuple[int, str, str]]:
    """Все, кого стоит показать: `(user_id, строка с HTML, подпись на кнопку)`.

    Владелец, пущенные через .env, одобренные — и отдельно все, у кого есть
    расход: доступ могли отозвать уже после трат, и тогда они пропадали бы.
    """
    owner_id = app.settings.owner_id
    seen: dict[int, tuple[str, str]] = {
        owner_id: (f"Владелец · <code>{owner_id}</code>", "Владелец")
    }
    for user_id in sorted(app.settings.allowed_user_ids - {owner_id}):
        seen[user_id] = (f"Из .env · <code>{user_id}</code>", f"id {user_id}")
    for record in await app.access.approved_users():
        seen.setdefault(record.user_id, (describe_user(record), user_title(record)))
    for user_id in sorted(await app.spenders()):
        seen.setdefault(user_id, (f"Без доступа · <code>{user_id}</code>", f"id {user_id}"))
    return [(uid, title, label) for uid, (title, label) in seen.items()]


@owner_only
async def cmd_grant(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = get_app(context)
    user_id = _parse_user_id(context)
    if user_id is None:
        await reply(update, "Как пользоваться: <code>/grant &lt;user_id&gt;</code>")
        return
    await app.access.approve(user_id)
    await _fetch_identity(context, app, user_id)
    record = await app.storage.get_user(user_id)
    who = describe_user(record) if record else f"<code>{user_id}</code>"
    await reply(update, f"✅ Доступ открыт: {who}")
    await _notify_user(context, user_id, "✅ Доступ к боту открыт. Пиши — я на связи.")


@owner_only
async def cmd_revoke(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = get_app(context)
    user_id = _parse_user_id(context)
    if user_id is None:
        await reply(update, "Как пользоваться: <code>/revoke &lt;user_id&gt;</code>")
        return
    await app.access.revoke(user_id)
    await reply(update, f"🚪 Доступ закрыт: <code>{user_id}</code>")


@owner_only
async def cmd_block(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = get_app(context)
    user_id = _parse_user_id(context)
    if user_id is None:
        await reply(update, "Как пользоваться: <code>/block &lt;user_id&gt;</code>")
        return
    await app.access.block(user_id)
    await reply(update, f"⛔️ Заблокирован: <code>{user_id}</code> (сообщения игнорируются)")


async def on_access_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопки «открыть доступ / отказать» под заявкой."""
    query = update.callback_query
    app = get_app(context)
    if query is None or not query.data:
        return
    actor = update.effective_user
    if actor is None or not app.access.is_owner(actor.id):
        await query.answer("Только владелец может решать", show_alert=True)
        return

    _, raw_id, decision = query.data.split(":", 2)
    user_id = int(raw_id)
    if decision == "approve":
        await app.access.approve(user_id)
        await _fetch_identity(context, app, user_id)
        await query.answer("Доступ открыт")
        await _edit(query, f"✅ Доступ открыт: <code>{user_id}</code>")
        await _notify_user(context, user_id, "✅ Доступ к боту открыт. Пиши — я на связи.")
    else:
        await app.access.block(user_id)
        await query.answer("Отказано")
        await _edit(query, f"⛔️ Отказано: <code>{user_id}</code>")


async def _edit(query, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    try:
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
    except Exception:  # noqa: BLE001 — в том числе «message is not modified»
        log.debug("Не смог обновить сообщение", exc_info=True)


async def _fetch_identity(context: ContextTypes.DEFAULT_TYPE, app, user_id: int) -> None:
    """Спросить имя у самого Telegram: `/grant` даёт нам только число.

    Раньше имя появлялось, лишь когда человек напишет сам, — выданный по id
    доступ оставлял в списках безымянную строку. Telegram отвечает, если человек
    когда-либо писал боту или есть общая группа; если не ответил — не беда.
    """
    try:
        chat = await context.bot.get_chat(user_id)
    except Exception:  # noqa: BLE001 — Telegram может не знать этого человека
        log.info("Telegram не отдал имя пользователя %s", user_id)
        return
    if chat.full_name or chat.username:
        await app.storage.update_identity(user_id, chat.username, chat.full_name)


async def _notify_user(context: ContextTypes.DEFAULT_TYPE, user_id: int, text: str) -> None:
    try:
        await context.bot.send_message(chat_id=user_id, text=text)
    except Exception:  # noqa: BLE001 — человек мог не начать диалог
        log.info("Не смог уведомить пользователя %s", user_id)


def _parse_user_id(context: ContextTypes.DEFAULT_TYPE) -> int | None:
    if not context.args:
        return None
    raw = context.args[0].strip().lstrip("@")
    try:
        return int(raw)
    except ValueError:
        return None
