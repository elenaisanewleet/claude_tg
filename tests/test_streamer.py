"""Проверяем живой рендер: одно сообщение правится, длинный ответ разбивается."""

from dataclasses import dataclass

import pytest

from claude_tg.storage import Prefs
from claude_tg.streamer import TelegramStreamer, describe_tool


@dataclass
class FakeMessage:
    message_id: int


class FakeBot:
    """Минимальная замена telegram.Bot: пишет всё в память."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.edits: list[tuple[int, str]] = []
        self._next_id = 100

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append(text)
        self._next_id += 1
        return FakeMessage(self._next_id)

    async def edit_message_text(self, chat_id, message_id, text, **kwargs):
        self.edits.append((message_id, text))
        return FakeMessage(message_id)

    @property
    def last(self) -> str:
        if self.edits:
            return self.edits[-1][1]
        return self.sent[-1] if self.sent else ""


def prefs(**overrides) -> Prefs:
    base = dict(
        model="opus",
        effort="high",
        permission_mode="default",
        thinking="adaptive",
        show_thinking=True,
        show_tools=True,
        show_footer=True,
    )
    base.update(overrides)
    return Prefs(**base)


@pytest.fixture
def bot() -> FakeBot:
    return FakeBot()


async def test_first_message_then_edits(bot):
    streamer = TelegramStreamer(bot, 1, prefs(), interval=0)
    await streamer.start()
    assert len(bot.sent) == 1

    await streamer.on_text("Привет, ")
    await streamer.on_text("мир")
    await streamer.finish()

    assert len(bot.sent) == 1, "новых сообщений быть не должно"
    assert "Привет, мир" in bot.last


async def test_long_answer_is_split_into_several_messages(bot):
    streamer = TelegramStreamer(bot, 1, prefs(show_footer=False), limit=900, interval=0)
    await streamer.start()
    for i in range(80):
        await streamer.on_text(f"строка номер {i} с некоторым текстом\n")
    await streamer.finish()

    assert len(bot.sent) > 1
    assert all(len(text) <= 4096 for text in bot.sent)
    assert all(len(text) <= 4096 for _, text in bot.edits)


async def test_tools_are_shown_and_hidden_by_pref(bot):
    streamer = TelegramStreamer(bot, 1, prefs(), interval=0)
    await streamer.start()
    await streamer.on_tool_use("t1", "Bash", {"command": "git status"})
    await streamer.on_text("готово")
    await streamer.finish()
    assert "git status" in bot.last

    quiet_bot = FakeBot()
    quiet = TelegramStreamer(quiet_bot, 1, prefs(show_tools=False), interval=0)
    await quiet.start()
    await quiet.on_tool_use("t1", "Bash", {"command": "git status"})
    await quiet.on_text("готово")
    await quiet.finish()
    assert "git status" not in quiet_bot.last


async def test_thinking_hidden_when_disabled(bot):
    streamer = TelegramStreamer(bot, 1, prefs(show_thinking=False), interval=0)
    await streamer.start()
    await streamer.on_thinking("секретные размышления")
    await streamer.on_text("ответ")
    await streamer.finish()
    assert "секретные размышления" not in bot.last
    assert "ответ" in bot.last


async def test_footer_shows_model_and_effort(bot):
    streamer = TelegramStreamer(bot, 1, prefs(model="sonnet", effort="xhigh"), interval=0)
    await streamer.start()
    await streamer.on_text("ок")
    await streamer.finish()
    assert "sonnet" in bot.last
    assert "xhigh" in bot.last


async def test_code_block_survives_streaming(bot):
    streamer = TelegramStreamer(bot, 1, prefs(), interval=0)
    await streamer.start()
    await streamer.on_text("Вот код:\n\n```python\nprint('привет')\n```\n")
    await streamer.finish()
    assert "<pre>" in bot.last
    assert 'class="language-python"' in bot.last


async def test_error_is_surfaced(bot):
    streamer = TelegramStreamer(bot, 1, prefs(), interval=0)
    await streamer.start()
    await streamer.on_error("⚠️ всё сломалось")
    await streamer.finish()
    assert "всё сломалось" in bot.last


def test_describe_tool_prefers_meaningful_arg():
    assert describe_tool("Bash", {"command": "ls -la"}) == "Bash: ls -la"
    assert describe_tool("Read", {"file_path": "/tmp/a.txt"}) == "Read: /tmp/a.txt"
    assert describe_tool("Weird", {}) == "Weird"
