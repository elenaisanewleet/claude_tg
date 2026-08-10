"""Поддержание актуальности Claude Code и SDK.

Бот — тонкая оболочка: вся «мозговая» часть живёт в CLI `claude` и в
`claude-agent-sdk`. Чтобы Telegram-клиент не отставал от десктопного Claude,
эти два компонента обновляются по расписанию и по команде `/update`.
"""

from __future__ import annotations

import asyncio
import html
import logging
import shutil
import sys
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

CLI_PACKAGE = "@anthropic-ai/claude-code"
SDK_PACKAGE = "claude-agent-sdk"
TIMEOUT = 600


@dataclass
class Step:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class UpdateReport:
    steps: list[Step] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    cli_before: str = ""
    cli_after: str = ""
    sdk_before: str = ""
    sdk_after: str = ""

    @property
    def changed(self) -> bool:
        return self.cli_before != self.cli_after or self.sdk_before != self.sdk_after

    def to_text(self) -> str:
        """HTML для Telegram. Всё, что пришло из вывода команд, экранируем:
        там встречаются `<`, `>` и `&`, на которых Telegram отвергает сообщение."""

        def esc(value: str) -> str:
            return html.escape(value or "", quote=False)

        def arrow(before: str, after: str) -> str:
            return f"<code>{esc(before) or '?'}</code> → <code>{esc(after) or '?'}</code>"

        lines = ["<b>Обновление компонентов</b>", ""]
        lines.append(f"Claude Code: {arrow(self.cli_before, self.cli_after)}")
        lines.append(f"claude-agent-sdk: {arrow(self.sdk_before, self.sdk_after)}")
        lines.append("")
        for step in self.steps:
            mark = "✅" if step.ok else "⚠️"
            detail = f" — {esc(step.detail)}" if step.detail else ""
            lines.append(f"{mark} {esc(step.name)}{detail}")
        for note in self.notes:
            lines.append("")
            lines.append(f"ℹ️ {esc(note)}")

        lines.append("")
        if self.changed:
            lines.append("♻️ Версии изменились — перезапусти бота, чтобы подхватить их.")
        else:
            lines.append("Версии не изменились.")
        return "\n".join(lines)


async def _run(cmd: list[str], timeout: int = TIMEOUT) -> tuple[int, str]:
    """Запустить процесс, вернуть (код возврата, слитый stdout+stderr)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError:
        return 127, f"команда не найдена: {cmd[0]}"
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        return 124, f"таймаут {timeout} c"
    return proc.returncode or 0, (out or b"").decode("utf-8", "replace").strip()


async def cli_version(cli_path: str | None = None) -> str:
    binary = cli_path or shutil.which("claude") or "claude"
    code, out = await _run([binary, "--version"], timeout=60)
    if code != 0:
        return ""
    return out.splitlines()[0].strip() if out else ""


def sdk_version() -> str:
    try:
        from importlib.metadata import version

        return version(SDK_PACKAGE)
    except Exception:  # noqa: BLE001 — пакет может стоять из исходников
        return ""


def bot_version() -> str:
    from . import __version__

    return __version__


async def latest_cli_version() -> str:
    """Что сейчас лежит в npm под тегом latest (для /version без обновления)."""
    if shutil.which("npm") is None:
        return ""
    code, out = await _run(["npm", "view", CLI_PACKAGE, "version"], timeout=120)
    return out.strip() if code == 0 else ""


async def brew_cask_name() -> str | None:
    """Каким каском Homebrew поставлен Claude Code, если поставлен им."""
    if shutil.which("brew") is None:
        return None
    code, out = await _run(["brew", "list", "--cask"], timeout=120)
    if code != 0:
        return None
    installed = set(out.split())
    for name in ("claude-code@latest", "claude-code"):
        if name in installed:
            return name
    return None


async def run_update(cli_path: str | None = None, *, with_sdk: bool = True) -> UpdateReport:
    """Обновить CLI и SDK. Ошибки не бросаем — складываем в отчёт."""
    report = UpdateReport()
    binary = cli_path or shutil.which("claude") or "claude"

    report.cli_before = await cli_version(binary)
    report.sdk_before = sdk_version()

    code, out = await _run([binary, "update"])
    if code == 0:
        report.steps.append(Step("claude update", True, _tail(out)))
    else:
        report.steps.append(Step("claude update", False, _tail(out)))
        if shutil.which("npm"):
            code, out = await _run(["npm", "install", "-g", f"{CLI_PACKAGE}@latest"])
            report.steps.append(Step("npm i -g claude-code@latest", code == 0, _tail(out)))

    # Если Claude Code поставлен Homebrew, его версией распоряжается брю:
    # `claude update` завершается успешно, но ничего не меняет.
    if await cli_version(binary) == report.cli_before:
        cask = await brew_cask_name()
        if cask:
            code, out = await _run(["brew", "upgrade", "--cask", cask])
            report.steps.append(Step(f"brew upgrade --cask {cask}", code == 0, _tail(out)))
            if cask == "claude-code":
                report.notes.append(
                    "Claude Code стоит из Homebrew, каск claude-code — это канал stable, "
                    "он отстаёт от свежих версий. Чаще обновляться: "
                    "brew uninstall --cask claude-code && brew install --cask claude-code@latest"
                )

    if with_sdk:
        code, out = await _run(
            [sys.executable, "-m", "pip", "install", "--upgrade", SDK_PACKAGE]
        )
        report.steps.append(Step(f"pip install -U {SDK_PACKAGE}", code == 0, _tail(out)))

    report.cli_after = await cli_version(binary)
    report.sdk_after = sdk_version()
    return report


def _tail(output: str, lines: int = 2, width: int = 160) -> str:
    if not output:
        return ""
    tail = " / ".join(output.strip().splitlines()[-lines:])
    return tail[:width]
