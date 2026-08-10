"""Опции, с которыми поднимается процесс Claude Code."""

from claude_tg.bridge import ClaudeSession
from claude_tg.storage import Prefs


def session(tmp_path, **overrides) -> ClaudeSession:
    base = dict(model="opus", effort="high", permission_mode="default", thinking="adaptive")
    base.update(overrides)
    return ClaudeSession("p1", tmp_path / "ws", Prefs(**base))


def test_options_carry_prefs(tmp_path):
    opts = session(tmp_path, model="sonnet", effort="xhigh", permission_mode="plan")._build_options(None)
    assert opts.model == "sonnet"
    assert opts.effort == "xhigh"
    assert opts.permission_mode == "plan"
    assert opts.cwd == str(tmp_path / "ws")


def test_system_prompt_uses_claude_code_preset(tmp_path):
    """Без пресета SDK отправит `--system-prompt ""` и обнулит поведение Claude Code."""
    opts = session(tmp_path)._build_options(None)
    assert opts.system_prompt == {"type": "preset", "preset": "claude_code"}
    assert opts.tools == {"type": "preset", "preset": "claude_code"}
    assert opts.skills == "all"
    assert opts.setting_sources == ["user", "project", "local"]


def test_streaming_and_checkpointing_enabled(tmp_path):
    opts = session(tmp_path)._build_options(None)
    assert opts.include_partial_messages is True
    assert opts.enable_file_checkpointing is True
    assert opts.can_use_tool is not None


def test_resume_is_passed_through(tmp_path):
    opts = session(tmp_path)._build_options("sess-1")
    assert opts.resume == "sess-1"
    assert opts.fork_session is False


def test_thinking_downgrades_on_max_effort(tmp_path):
    opts = session(tmp_path, thinking="disabled", effort="max")._build_options(None)
    assert opts.thinking["type"] == "adaptive"

    opts = session(tmp_path, thinking="disabled", effort="medium")._build_options(None)
    assert opts.thinking == {"type": "disabled"}


def test_workspace_dirs_created(tmp_path):
    ws = tmp_path / "ws"
    session(tmp_path)._build_options(None)
    assert (ws / "inbox").is_dir()
    assert (ws / "outbox").is_dir()
