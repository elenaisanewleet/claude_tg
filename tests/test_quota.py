"""Лимиты расхода: владельцу без ограничений, гостям — потолок."""

import time

import pytest

from claude_tg.access import AccessControl
from claude_tg.app import AppContext, Quota
from claude_tg.bridge import SessionManager
from claude_tg.config import Settings
from claude_tg.storage import Storage

OWNER = 100
GUEST = 200


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


async def test_owner_is_unlimited_by_default(app):
    quota = await app.quota_for(OWNER)
    assert quota.unlimited is True
    assert quota.allowed is True
    assert "без ограничений" in quota.describe()


async def test_guest_gets_the_default_budget(app):
    quota = await app.quota_for(GUEST)
    assert quota.limit == 5.0
    assert quota.spent == 0.0
    assert quota.allowed is True


async def test_spending_accumulates_and_blocks(app):
    await app.record_turn(GUEST, 2.0, "opus")
    await app.record_turn(GUEST, 2.5, "opus")

    quota = await app.quota_for(GUEST)
    assert quota.spent == pytest.approx(4.5)
    assert quota.allowed is True
    assert quota.left == pytest.approx(0.5)

    await app.record_turn(GUEST, 1.0, "opus")
    quota = await app.quota_for(GUEST)
    assert quota.allowed is False
    assert "Лимит исчерпан" in quota.refusal()


async def test_owner_never_blocked_however_much_spent(app):
    await app.record_turn(OWNER, 999.0, "opus")
    quota = await app.quota_for(OWNER)
    assert quota.allowed is True
    assert quota.spent == pytest.approx(999.0)


async def test_spending_outside_window_does_not_count(app):
    long_ago = int(time.time()) - 200 * 3600  # окно 168 часов
    app.storage.conn.execute(
        "INSERT INTO usage (user_id, at, cost_usd, model) VALUES (?, ?, ?, ?)",
        (GUEST, long_ago, 99.0, "opus"),
    )
    app.storage.conn.commit()

    quota = await app.quota_for(GUEST)
    assert quota.spent == 0.0
    assert quota.allowed is True


async def test_explicit_unlimited_beats_default(app):
    await app.record_turn(GUEST, 50.0, "opus")
    assert (await app.quota_for(GUEST)).allowed is False

    await app.set_budget(GUEST, None)
    quota = await app.quota_for(GUEST)
    assert quota.unlimited is True
    assert quota.allowed is True


async def test_explicit_budget_and_reset(app):
    await app.set_budget(GUEST, 20.0)
    assert (await app.quota_for(GUEST)).limit == 20.0

    await app.reset_budget(GUEST)
    assert (await app.quota_for(GUEST)).limit == 5.0


async def test_owner_can_be_limited_explicitly(app):
    """Владелец без ограничений по умолчанию, но может ограничить и себя."""
    await app.set_budget(OWNER, 1.0)
    await app.record_turn(OWNER, 2.0, "opus")

    quota = await app.quota_for(OWNER)
    assert quota.unlimited is False
    assert quota.allowed is False


async def test_free_turns_are_not_recorded(app):
    await app.record_turn(GUEST, 0.0, "opus")
    assert (await app.quota_for(GUEST)).spent == 0.0


async def test_refusal_mentions_when_it_frees_up(app):
    await app.record_turn(GUEST, 10.0, "opus")
    quota = await app.quota_for(GUEST)
    assert quota.frees_up_in is not None
    assert "освободится" in quota.refusal()


def test_quota_formatting_is_readable():
    quota = Quota(limit=10.0, spent=2.5, window_hours=168)
    text = quota.describe()
    assert "$2.50" in text
    assert "$10.00" in text
    assert "7 дн." in text
    assert "25%" in text

    hourly = Quota(limit=1.0, spent=0.0, window_hours=5)
    assert "5 ч." in hourly.describe()
