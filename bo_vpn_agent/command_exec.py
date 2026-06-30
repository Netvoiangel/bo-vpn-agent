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
    output_truncated: bool = False

    @property
    def exit_code(self) -> int:
        return self.returncode

    @property
    def timeout(self) -> bool:
        return self.timed_out


class CommandExecutor(Protocol):
    def run(self, args: Sequence[str], timeout_sec: int) -> CommandResult:
        ...


class SubprocessCommandExecutor:
    def __init__(self, max_output_bytes: int = 64 * 1024) -> None:
        self.max_output_bytes = max_output_bytes

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
                stdout=_limit_text(completed.stdout, self.max_output_bytes),
                stderr=_limit_text(completed.stderr, self.max_output_bytes),
                output_truncated=_is_truncated(completed.stdout, self.max_output_bytes)
                or _is_truncated(completed.stderr, self.max_output_bytes),
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout if isinstance(exc.stdout, str) else ""
            stderr = exc.stderr if isinstance(exc.stderr, str) else ""
            return CommandResult(
                args=normalized_args,
                returncode=124,
                stdout=_limit_text(stdout, self.max_output_bytes),
                stderr=_limit_text(stderr, self.max_output_bytes),
                timed_out=True,
                output_truncated=_is_truncated(stdout, self.max_output_bytes) or _is_truncated(stderr, self.max_output_bytes),
            )


def _limit_text(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _is_truncated(value: str, max_bytes: int) -> bool:
    return len(value.encode("utf-8")) > max_bytes
