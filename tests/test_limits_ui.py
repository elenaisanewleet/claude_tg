"""Кнопочный интерфейс лимитов: выбрать человека и поменять ему потолок."""

import pytest

from claude_tg import ui
from claude_tg.access import AccessControl, user_title
from claude_tg.app import AppContext
from claude_tg.bridge import SessionManager
from claude_tg.config import Settings
from claude_tg.handlers.admin import (
    _fetch_identity,
    _roster,
    limit_card_screen,
    limits_screen,
)
from claude_tg.storage import Storage, UserRecord

OWNER = 100
GUEST = 200

CALLBACK_LIMIT = 64  # Telegram: callback_data не длиннее 64 байт


class FakeUser:
    """То, что приходит от Telegram, — нам хватает четырёх полей."""

    is_bot = False

    def __init__(self, user_id: int, username: str | None, full_name: str | None) -> None:
        self.id = user_id
        self.username = username
        self.full_name = full_name


@pytest.fixture
async def app(tmp_path):
    settings = Settings.from_env(
        {
            "TELEGRAM_BOT_TOKEN": "1:a",
            "OWNER_USER_ID": str(OWNER),
            "DATA_DIR": str(tmp_path),
            "DEFAULT_BUDGET_USD": "5",
            "BUDGET_WINDOW_HOURS": "168",
        }
    )
    storage = Storage(settings.db_path)
    await storage.connect()
    context = AppContext(
        settings=settings,
        storage=storage,
        access=AccessControl(settings, storage),
        sessions=SessionManager(settings.workspace_root),
    )
    yield context
    await storage.close()


async def test_name_appears_after_grant_by_id(app):
    """Доступ выдали по голому id — имя должно появиться, как только человек напишет."""
    await app.access.approve(GUEST)
    record = (await app.access.approved_users())[0]
    assert record.full_name is None

    decision = await app.access.check(FakeUser(GUEST, "olga", "Ольга Петрова"))
    assert decision.allowed is True

    record = (await app.access.approved_users())[0]
    assert record.full_name == "Ольга Петрова"
    assert record.username == "olga"


async def test_renamed_user_is_refreshed(app):
    await app.access.approve(GUEST)
    await app.access.check(FakeUser(GUEST, "olga", "Ольга"))
    await app.access.check(FakeUser(GUEST, "olga_p", "Ольга П."))

    record = (await app.access.approved_users())[0]
    assert record.username == "olga_p"
    assert record.full_name == "Ольга П."


async def test_refreshing_name_keeps_access(app):
    """Освежение имени не должно ронять человека обратно в заявки."""
    await app.access.approve(GUEST)
    await app.access.check(FakeUser(GUEST, "olga", "Ольга"))
    assert (await app.storage.get_user(GUEST)).status == "approved"


async def test_roster_gives_plain_label_for_button(app):
    await app.access.approve(GUEST)
    await app.access.check(FakeUser(GUEST, "m&m", "Аня <3"))

    entry = next(e for e in await _roster(app) if e[0] == GUEST)
    _, html_title, button_label = entry

    # В текст сообщения уходит экранированное, на кнопку — как есть.
    assert "&lt;3" in html_title
    assert button_label == "Аня <3"


async def test_limits_screen_has_a_button_per_person(app):
    await app.access.approve(GUEST)
    await app.access.check(FakeUser(GUEST, "olga", "Ольга"))

    text, markup = await limits_screen(app)
    targets = [b.callback_data for row in markup.inline_keyboard for b in row]

    assert f"lim:u:{OWNER}" in targets
    assert f"lim:u:{GUEST}" in targets
    assert "Ольга" in "".join(b.text for row in markup.inline_keyboard for b in row)
    assert "Расход и лимиты" in text


async def test_card_marks_the_active_preset(app):
    await app.set_budget(GUEST, 20.0)
    _, markup = await limit_card_screen(app, GUEST)
    labels = [b.text for row in markup.inline_keyboard for b in row]

    assert "✓ $20" in labels
    assert "$5" in labels  # остальные без галочки


async def test_card_marks_unlimited(app):
    await app.set_budget(GUEST, None)
    _, markup = await limit_card_screen(app, GUEST)
    labels = [b.text for row in markup.inline_keyboard for b in row]

    assert "✓ ♾ Без лимита" in labels


async def test_card_shows_spending(app):
    await app.record_turn(GUEST, 1.25, "opus")
    text, _ = await limit_card_screen(app, GUEST)
    assert "$1.25" in text


def test_callback_data_fits_telegram_limit():
    """user_id в Telegram бывает десятизначным — строка обязана влезть в 64 байта."""
    huge = 9999999999
    markup = ui.limit_card(huge, unlimited=False, limit=None)
    for row in markup.inline_keyboard:
        for button in row:
            assert len(button.callback_data.encode()) <= CALLBACK_LIMIT

    entries = [(huge, "✅ Человек с очень длинным именем", "$1.00 из $5.00")]
    for row in ui.limits_menu(entries).inline_keyboard:
        for button in row:
            assert len(button.callback_data.encode()) <= CALLBACK_LIMIT


def test_long_names_are_clipped_on_buttons():
    entries = [(1, "✅ " + "а" * 100, "$0.00 из $5.00")]
    markup = ui.limits_menu(entries)
    assert len(markup.inline_keyboard[0][0].text) < 60


class FakeChat:
    def __init__(self, username: str | None, full_name: str | None) -> None:
        self.username = username
        self.full_name = full_name


class FakeBot:
    """Telegram отвечает на get_chat, если человек когда-то писал боту."""

    def __init__(self, chat: FakeChat | None = None, fails: bool = False) -> None:
        self._chat = chat
        self._fails = fails

    async def get_chat(self, chat_id: int) -> FakeChat:
        if self._fails:
            raise RuntimeError("Bad Request: chat not found")
        return self._chat


class FakeContext:
    def __init__(self, bot: FakeBot) -> None:
        self.bot = bot


async def test_grant_by_id_picks_up_the_name_from_telegram(app):
    """Главное: имя должно появиться сразу, а не ждать первого сообщения."""
    await app.access.approve(GUEST)
    assert (await app.storage.get_user(GUEST)).full_name is None

    context = FakeContext(FakeBot(FakeChat("kamilavanila", "Kamila")))
    await _fetch_identity(context, app, GUEST)

    record = await app.storage.get_user(GUEST)
    assert record.full_name == "Kamila"
    assert record.username == "kamilavanila"
    assert record.status == "approved"


async def test_grant_survives_telegram_not_knowing_the_person(app):
    await app.access.approve(GUEST)
    context = FakeContext(FakeBot(fails=True))

    await _fetch_identity(context, app, GUEST)  # молча переживаем отказ

    assert (await app.storage.get_user(GUEST)).status == "approved"


async def test_empty_answer_does_not_wipe_a_known_name(app):
    await app.access.approve(GUEST)
    await app.access.check(FakeUser(GUEST, "olga", "Ольга"))

    context = FakeContext(FakeBot(FakeChat(None, None)))
    await _fetch_identity(context, app, GUEST)

    assert (await app.storage.get_user(GUEST)).full_name == "Ольга"


def test_user_title_falls_back_to_username_then_id():
    def record(username, full_name):
        return UserRecord(
            user_id=42,
            username=username,
            full_name=full_name,
            status="approved",
        )

    assert user_title(record("olga", "Ольга")) == "Ольга"
    assert user_title(record("olga", None)) == "@olga"
    assert user_title(record(None, None)) == "id 42"
