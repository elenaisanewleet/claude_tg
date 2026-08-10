"""Разбор потока сообщений SDK: что именно долетает до Telegram."""

from pathlib import Path

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from claude_tg.bridge import ClaudeSession, TurnListener, _preview_tool_result
from claude_tg.storage import Prefs


class Recorder(TurnListener):
    def __init__(self) -> None:
        self.text: list[str] = []
        self.thinking: list[str] = []
        self.tools: list[tuple[str, str]] = []
        self.results: list[tuple[str, bool, str]] = []
        self.systems: list[str] = []
        self.errors: list[str] = []
        self.done: list[ResultMessage] = []

    async def on_text(self, delta: str) -> None:
        self.text.append(delta)

    async def on_thinking(self, delta: str) -> None:
        self.thinking.append(delta)

    async def on_tool_use(self, tool_id, name, tool_input) -> None:
        self.tools.append((tool_id, name))

    async def on_tool_result(self, tool_id, is_error, preview) -> None:
        self.results.append((tool_id, is_error, preview))

    async def on_system(self, subtype, data) -> None:
        self.systems.append(subtype)

    async def on_error(self, message: str) -> None:
        self.errors.append(message)

    async def on_result(self, result) -> None:
        self.done.append(result)


def make_session(tmp_path: Path) -> ClaudeSession:
    prefs = Prefs(model="opus", effort="high", permission_mode="default", thinking="adaptive")
    return ClaudeSession("p1", tmp_path / "ws", prefs)


def delta(kind: str, value: str) -> StreamEvent:
    key = "text" if kind == "text_delta" else "thinking"
    return StreamEvent(
        uuid="u1",
        session_id="s1",
        event={"type": "content_block_delta", "delta": {"type": kind, key: value}},
        parent_tool_use_id=None,
    )


def assistant(*blocks) -> AssistantMessage:
    return AssistantMessage(content=list(blocks), model="claude-opus-5", parent_tool_use_id=None)


@pytest.fixture
def session(tmp_path):
    return make_session(tmp_path)


async def test_text_deltas_reach_listener(session):
    rec = Recorder()
    await session._dispatch(delta("text_delta", "Привет"), rec)
    await session._dispatch(delta("text_delta", ", мир"), rec)
    assert "".join(rec.text) == "Привет, мир"


async def test_thinking_deltas_are_separate(session):
    rec = Recorder()
    await session._dispatch(delta("thinking_delta", "прикидываю"), rec)
    assert rec.thinking == ["прикидываю"]
    assert rec.text == []


async def test_streamed_text_is_not_duplicated_by_final_message(session):
    rec = Recorder()
    await session._dispatch(delta("text_delta", "Ответ целиком"), rec)
    await session._dispatch(assistant(TextBlock(text="Ответ целиком")), rec)
    assert "".join(rec.text) == "Ответ целиком"


async def test_text_without_deltas_still_arrives(session):
    """Если частичный стрим недоступен, текст берём из финального сообщения."""
    rec = Recorder()
    await session._dispatch(assistant(TextBlock(text="Только финал")), rec)
    assert rec.text == ["Только финал"]


async def test_thinking_block_fallback(session):
    rec = Recorder()
    await session._dispatch(assistant(ThinkingBlock(thinking="дума", signature="sig")), rec)
    assert rec.thinking == ["дума"]


async def test_tool_use_and_error_result(session):
    rec = Recorder()
    await session._dispatch(
        assistant(ToolUseBlock(id="t1", name="Bash", input={"command": "ls"})), rec
    )
    await session._dispatch(
        UserMessage(content=[ToolResultBlock(tool_use_id="t1", content="boom", is_error=True)]),
        rec,
    )
    assert rec.tools == [("t1", "Bash")]
    assert rec.results == [("t1", True, "boom")]


async def test_api_error_is_reported(session):
    rec = Recorder()
    message = assistant(TextBlock(text=""))
    message.error = "rate_limit"
    await session._dispatch(message, rec)
    assert rec.errors and "rate_limit" in rec.errors[0]


async def test_init_system_message_captures_session_id(session):
    rec = Recorder()
    await session._dispatch(
        SystemMessage(subtype="init", data={"session_id": "abc-123", "tools": ["Bash", "Read"]}),
        rec,
    )
    assert session.session_id == "abc-123"
    assert session.available_tools == ["Bash", "Read"]
    assert rec.systems == ["init"]


async def test_result_updates_stats(session):
    rec = Recorder()
    result = ResultMessage(
        subtype="success",
        duration_ms=1200,
        duration_api_ms=1000,
        is_error=False,
        num_turns=1,
        session_id="abc-123",
        total_cost_usd=0.0042,
    )
    returned = await session._dispatch(result, rec)
    assert returned is result
    assert rec.done == [result]
    assert session.stats.turns == 1
    assert session.stats.total_cost_usd == pytest.approx(0.0042)
    assert session.session_id == "abc-123"


async def test_apply_prefs_without_client_just_stores(session):
    new = Prefs(model="sonnet", effort="max", permission_mode="plan", thinking="adaptive")
    restarted = await session.apply_prefs(new)
    assert restarted is False
    assert session.prefs.model == "sonnet"
    assert session.prefs.effort == "max"


def test_preview_tool_result_handles_shapes():
    assert _preview_tool_result(None) == ""
    assert _preview_tool_result("строка") == "строка"
    assert _preview_tool_result([{"type": "text", "text": "раз"}, {"type": "image"}]) == "раз\n[image]"
