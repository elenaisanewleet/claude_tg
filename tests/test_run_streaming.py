"""Живой вывод команд: проверяем на настоящих процессах, а не на заглушках."""

import sys

import pytest

from claude_tg.updater import _run


async def test_collects_output_and_exit_code():
    code, out = await _run([sys.executable, "-c", "print('привет')"])
    assert code == 0
    assert out == "привет"


async def test_nonzero_exit_is_reported():
    code, out = await _run([sys.executable, "-c", "import sys; sys.exit(3)"])
    assert code == 3


async def test_stderr_is_merged_in():
    code, out = await _run(
        [sys.executable, "-c", "import sys; print('ошибка', file=sys.stderr)"]
    )
    assert code == 0
    assert "ошибка" in out


async def test_lines_arrive_while_process_runs():
    seen: list[str] = []

    async def on_line(line: str) -> None:
        seen.append(line)

    code, out = await _run(
        [sys.executable, "-c", "import sys\nfor i in range(3):\n    print(i); sys.stdout.flush()"],
        on_line=on_line,
    )
    assert code == 0
    assert seen == ["0", "1", "2"]


async def test_carriage_returns_are_treated_as_line_breaks():
    """brew и pip рисуют прогресс через \\r — без этого не видно ни строчки."""
    seen: list[str] = []

    async def on_line(line: str) -> None:
        seen.append(line)

    await _run(
        [sys.executable, "-c", r"import sys; sys.stdout.write('10%\r50%\r100%\n')"],
        on_line=on_line,
    )
    assert seen == ["10%", "50%", "100%"]


async def test_missing_binary_is_not_an_exception():
    code, out = await _run(["definitely-not-a-real-binary-xyz"])
    assert code == 127
    assert "не найдена" in out


async def test_timeout_kills_the_process():
    code, out = await _run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=1)
    assert code == 124
    assert "таймаут" in out


async def test_progress_callback_failure_propagates_to_caller():
    """`_run` не глотает ошибки колбэка — это делает уровнем выше, осознанно."""

    async def broken(line: str) -> None:
        raise RuntimeError("сломался")

    with pytest.raises(RuntimeError):
        await _run([sys.executable, "-c", "print('раз')"], on_line=broken)
