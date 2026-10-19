"""Запуск внешних команд. Абстракция нужна для тестов doctor: в них подставляется фейковый runner."""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


Runner = Callable[[Sequence[str]], CommandResult]

NOT_FOUND = 127
TIMED_OUT = 124


def run_command(
    args: Sequence[str], *, timeout_s: float = 30, env: Mapping[str, str] | None = None
) -> CommandResult:
    try:
        proc = subprocess.run(
            list(args), capture_output=True, text=True, timeout=timeout_s, check=False, env=env
        )
    except FileNotFoundError:
        return CommandResult(NOT_FOUND, "", f"{args[0]}: команда не найдена")
    except subprocess.TimeoutExpired:
        return CommandResult(TIMED_OUT, "", f"{args[0]}: не завершилась за {timeout_s} с")
    return CommandResult(proc.returncode, proc.stdout, proc.stderr)
