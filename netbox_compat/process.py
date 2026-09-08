"""Запуск внешних команд с логом в файл и коротким хвостом для отчёта.

Полный вывод стадии уходит в отдельный файл, в отчёт попадают только
последние строки — иначе JSON распухает на порядки от tracebacks Django.
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

TAIL_LINES = 20


@dataclass
class CommandResult:
    args: list[str]
    returncode: int
    duration: float
    log_path: Path
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    stdout_tail: list[str] = field(default_factory=list)
    stderr_tail: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


def tail(text: str, limit: int = TAIL_LINES) -> list[str]:
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    return lines[-limit:]


def run_logged(
    args: Sequence[str],
    log_path: Path,
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
    append: bool = True,
) -> CommandResult:
    """Выполнить команду, дописав её вывод в log_path."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    argv = [str(a) for a in args]
    started = time.monotonic()
    timed_out = False

    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd) if cwd else None,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        returncode, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        returncode = 124
        stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode(errors="replace")
        stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode(errors="replace")
        stderr += f"\ncommand timed out after {timeout}s\n"
    except OSError as exc:
        timed_out = False
        returncode = 127
        stdout = ""
        stderr = f"failed to execute {argv[0]!r}: {exc}\n"

    duration = time.monotonic() - started
    with log_path.open("a" if append else "w", encoding="utf-8") as fh:
        fh.write(f"$ {' '.join(argv)}\n")
        if cwd:
            fh.write(f"# cwd: {cwd}\n")
        if stdout:
            fh.write("--- stdout ---\n" + stdout)
            if not stdout.endswith("\n"):
                fh.write("\n")
        if stderr:
            fh.write("--- stderr ---\n" + stderr)
            if not stderr.endswith("\n"):
                fh.write("\n")
        fh.write(f"--- exit {returncode} in {duration:.1f}s ---\n\n")

    return CommandResult(
        args=argv,
        returncode=returncode,
        duration=duration,
        log_path=log_path,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
        stdout_tail=tail(stdout),
        stderr_tail=tail(stderr),
    )


def clean_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Окружение для дочерних процессов без прокси и без VIRTUAL_ENV раннера."""
    env = dict(os.environ)
    for key in ("VIRTUAL_ENV", "PYTHONHOME", "PYTHONPATH"):
        env.pop(key, None)
    if extra:
        env.update(extra)
    return env
