from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Protocol, Sequence


@dataclass(frozen=True, slots=True)
class CommandResult:
    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


class CommandExecutor(Protocol):
    def run(self, args: Sequence[str], timeout_sec: int) -> CommandResult:
        ...


class SubprocessCommandExecutor:
    def run(self, args: Sequence[str], timeout_sec: int) -> CommandResult:
        normalized_args = tuple(str(arg) for arg in args)
        try:
            completed = subprocess.run(
                normalized_args,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout_sec,
            )
            return CommandResult(
                args=normalized_args,
                returncode=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout if isinstance(exc.stdout, str) else ""
            stderr = exc.stderr if isinstance(exc.stderr, str) else ""
            return CommandResult(
                args=normalized_args,
                returncode=124,
                stdout=stdout,
                stderr=stderr,
                timed_out=True,
            )
