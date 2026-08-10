import pytest

from claude_tg.storage import (
    STATUS_APPROVED,
    STATUS_BLOCKED,
    STATUS_PENDING,
    Prefs,
    Storage,
)


@pytest.fixture
async def storage(tmp_path):
    store = Storage(tmp_path / "test.sqlite3")
    await store.connect()
    yield store
    await store.close()


def default_prefs() -> Prefs:
    return Prefs(model="opus", effort="high", permission_mode="default", thinking="adaptive")


async def test_prefs_roundtrip(storage):
    prefs = await storage.get_prefs("p1", default_prefs())
    assert prefs.model == "opus"

    prefs.model = "sonnet"
    prefs.effort = "max"
    prefs.show_tools = False
    await storage.save_prefs("p1", prefs)

    loaded = await storage.get_prefs("p1", default_prefs())
    assert loaded.model == "sonnet"
    assert loaded.effort == "max"
    assert loaded.show_tools is False


async def test_prefs_are_isolated_per_chat(storage):
    prefs = default_prefs()
    prefs.model = "fable"
    await storage.save_prefs("p1", prefs)

    other = await storage.get_prefs("p2", default_prefs())
    assert other.model == "opus"


async def test_prefs_survive_new_fields(storage):
    # В БД лежит старая запись без части полей — не должно падать.
    await storage.set_kv("noop", "1")
    storage.conn.execute(
        "INSERT INTO prefs (scope, payload) VALUES (?, ?)", ("legacy", '{"model": "haiku"}')
    )
    storage.conn.commit()

    loaded = await storage.get_prefs("legacy", default_prefs())
    assert loaded.model == "haiku"
    assert loaded.effort == "high"  # подставилось из умолчаний


async def test_user_lifecycle(storage):
    await storage.upsert_user(42, "lena", "Лена", STATUS_PENDING)
    record = await storage.get_user(42)
    assert record is not None and record.status == STATUS_PENDING

    await storage.set_status(42, STATUS_APPROVED)
    assert (await storage.get_user(42)).status == STATUS_APPROVED
    assert [u.user_id for u in await storage.list_users(STATUS_APPROVED)] == [42]

    await storage.set_status(42, STATUS_BLOCKED)
    assert await storage.list_users(STATUS_APPROVED) == []

    await storage.forget_user(42)
    assert await storage.get_user(42) is None


async def test_chat_session_roundtrip(storage):
    await storage.save_chat_session("p1", "/tmp/ws", None, 42)
    stored = await storage.get_chat_session("p1")
    assert stored is not None
    assert stored.session_id is None
    assert stored.workspace == "/tmp/ws"

    await storage.save_chat_session("p1", "/tmp/ws", "sess-123", 42, title="Тест")
    stored = await storage.get_chat_session("p1")
    assert stored.session_id == "sess-123"
    assert stored.title == "Тест"

    await storage.clear_chat_session("p1")
    assert await storage.get_chat_session("p1") is None


async def test_kv(storage):
    assert await storage.get_kv("missing") is None
    await storage.set_kv("k", "v1")
    await storage.set_kv("k", "v2")
    assert await storage.get_kv("k") == "v2"
