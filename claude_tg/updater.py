"""Поддержание актуальности Claude Code и SDK.

Бот — тонкая оболочка: вся «мозговая» часть живёт в CLI `claude` и в
`claude-agent-sdk`. Чтобы Telegram-клиент не отставал от десктопного Claude,
эти два компонента обновляются по расписанию и по команде `/update`.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import shutil
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

CLI_PACKAGE = "@anthropic-ai/claude-code"
SDK_PACKAGE = "claude-agent-sdk"
TIMEOUT = 600

_LINE_BREAK = re.compile(r"[\r\n]")


@dataclass
class ProgressEvent:
    """Состояние обновления для показа пользователю."""

    index: int  # номер текущего шага, с единицы
    total: int  # сколько шагов запланировано
    label: str  # что выполняется прямо сейчас
    line: str = ""  # последняя строка вывода команды
    elapsed: float = 0.0  # секунд с начала обновления

    @property
    def fraction(self) -> float:
        """Доля выполненного: шаг считается завершённым, когда начался следующий."""
        if self.total <= 0:
            return 0.0
        return min(1.0, max(0.0, (self.index - 1) / self.total))


# Куда сообщать о ходе дела (например, правкой сообщения в Telegram).
Progress = Callable[[ProgressEvent], Awaitable[None]] | None


async def _announce(progress: Progress, event: ProgressEvent) -> None:
    """Сообщить о прогрессе. Не даём косметике уронить обновление."""
    if progress is None:
        return
    try:
        await progress(event)
    except Exception:  # noqa: BLE001
        log.debug("Не смог показать прогресс обновления", exc_info=True)


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


async def _terminate(proc: asyncio.subprocess.Process) -> None:
    """Добить процесс и дождаться его, чтобы не оставить зомби и открытые пайпы."""
    if proc.returncode is not None:
        return
    try:
        proc.kill()
    except ProcessLookupError:
        return
    try:
        await proc.wait()
    except Exception:  # noqa: BLE001 — процесс уже мог уйти
        log.debug("Не смог дождаться завершения процесса", exc_info=True)


async def _run(
    cmd: list[str],
    timeout: int = TIMEOUT,
    on_line: Callable[[str], Awaitable[None]] | None = None,
) -> tuple[int, str]:
    """Запустить процесс, вернуть (код возврата, слитый stdout+stderr).

    Читаем вывод кусками, а не построчно: brew и pip рисуют прогресс через
    возврат каретки, и `readline()` на таком выводе молчал бы до самого конца.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            limit=1024 * 1024,
        )
    except FileNotFoundError:
        return 127, f"команда не найдена: {cmd[0]}"

    collected: list[str] = []
    pending = ""

    async def pump() -> None:
        nonlocal pending
        assert proc.stdout is not None
        while True:
            data = await proc.stdout.read(4096)
            if not data:
                break
            text = data.decode("utf-8", "replace")
            collected.append(text)
            if on_line is None:
                continue
            parts = _LINE_BREAK.split(pending + text)
            pending = parts.pop()
            for part in parts:
                part = part.strip()
                if part:
                    await on_line(part)

    try:
        async with asyncio.timeout(timeout):
            await pump()
            await proc.wait()
    except TimeoutError:
        await _terminate(proc)
        return 124, f"таймаут {timeout} c"
    except BaseException:
        # Колбэк упал или ход отменили — не оставляем процесс жить дальше.
        await _terminate(proc)
        raise

    if on_line is not None and pending.strip():
        await on_line(pending.strip())
    return proc.returncode or 0, "".join(collected).strip()


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


async def run_update(
    cli_path: str | None = None,
    *,
    with_sdk: bool = True,
    progress: Progress = None,
) -> UpdateReport:
    """Обновить CLI и SDK. Ошибки не бросаем — складываем в отчёт.

    `progress` вызывается перед каждым шагом: обновление занимает минуты, и без
    этого непонятно, работает оно ещё или зависло.
    """
    report = UpdateReport()
    binary = cli_path or shutil.which("claude") or "claude"

    report.cli_before = await cli_version(binary)
    report.sdk_before = sdk_version()

    # План считаем заранее, чтобы шкала прогресса не врала. Если Claude Code
    # поставлен каском, его версией распоряжается brew — `claude update` там
    # завершается успешно и ничего не меняет, поэтому brew нужен всегда.
    cask = await brew_cask_name()
    plan = ["claude update"]
    if cask:
        plan.append(f"brew upgrade --cask {cask}")
    if with_sdk:
        plan.append(f"pip install -U {SDK_PACKAGE}")
    total = len(plan)
    started = time.monotonic()

    async def run_step(index: int, label: str, cmd: list[str]) -> tuple[int, str]:
        await _announce(progress, ProgressEvent(index, total, label, "", _since(started)))

        async def on_line(line: str) -> None:
            await _announce(progress, ProgressEvent(index, total, label, line, _since(started)))

        return await _run(cmd, on_line=on_line if progress else None)

    step = 1
    code, out = await run_step(step, "claude update", [binary, "update"])
    report.steps.append(Step("claude update", code == 0, _tail(out)))
    if code != 0 and not cask and shutil.which("npm"):
        code, out = await run_step(
            step, "npm install -g claude-code", ["npm", "install", "-g", f"{CLI_PACKAGE}@latest"]
        )
        report.steps.append(Step("npm i -g claude-code@latest", code == 0, _tail(out)))
    step += 1

    if cask:
        label = f"brew upgrade --cask {cask}"
        code, out = await run_step(step, label, ["brew", "upgrade", "--cask", cask])
        report.steps.append(Step(label, code == 0, _tail(out)))
        if cask == "claude-code":
            report.notes.append(
                "Claude Code стоит из Homebrew, каск claude-code — это канал stable, "
                "он отстаёт от свежих версий. Чаще обновляться: "
                "brew uninstall --cask claude-code && brew install --cask claude-code@latest"
            )
        step += 1

    if with_sdk:
        label = f"pip install -U {SDK_PACKAGE}"
        code, out = await run_step(
            step, label, [sys.executable, "-m", "pip", "install", "--upgrade", SDK_PACKAGE]
        )
        report.steps.append(Step(label, code == 0, _tail(out)))
        step += 1

    report.cli_after = await cli_version(binary)
    report.sdk_after = sdk_version()
    await _announce(progress, ProgressEvent(total + 1, total, "готово", "", _since(started)))
    return report


def _since(started: float) -> float:
    return time.monotonic() - started


def _tail(output: str, lines: int = 2, width: int = 160) -> str:
    if not output:
        return ""
    tail = " / ".join(output.strip().splitlines()[-lines:])
    return tail[:width]
