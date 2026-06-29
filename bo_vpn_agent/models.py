from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


TASK_STATES = {
    "created",
    "queued",
    "starting_vpn",
    "vpn_connected",
    "checking_vehicle",
    "running_operation",
    "collecting_result",
    "cleanup",
    "finished",
    "failed",
    "timeout",
}

TERMINAL_STATES = {"finished", "failed", "timeout"}


def utc_now() -> datetime:
    return datetime.now(UTC)


def isoformat(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


@dataclass(slots=True)
class Vehicle:
    number: str
    ip: str

    def to_response(self) -> dict[str, str]:
        return {"number": self.number, "ip": self.ip}


@dataclass(slots=True)
class VpnCredentials:
    mode: str
    username: str
    password: str


@dataclass(slots=True)
class Task:
    task_id: str
    request_id: str
    telegram_user_id: int
    user_role: str
    vehicle: Vehicle
    operation: str
    params: dict[str, Any]
    timeout_sec: int
    request_fingerprint: str
    runner_mode: str
    risk: str
    state_changing: bool = False
    state: str = "created"
    phase: str = "created"
    phase_message: str = "Задача создана"
    summary: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error_code: str | None = None
    message: str | None = None
    cleanup_failed: bool = False
    created_at: datetime = field(default_factory=utc_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    updated_at: datetime = field(default_factory=utc_now)

    @property
    def duration_sec(self) -> int | None:
        end = self.finished_at or utc_now()
        start = self.started_at or self.created_at
        if self.state in TERMINAL_STATES or self.started_at is not None:
            return max(0, int((end - start).total_seconds()))
        return None

    def set_state(self, state: str, message: str | None = None) -> None:
        if state not in TASK_STATES:
            raise ValueError(f"unknown task state: {state}")
        self.state = state
        self.phase = state
        if message is not None:
            self.phase_message = message
        self.updated_at = utc_now()
        if state != "created" and self.started_at is None:
            self.started_at = self.updated_at
        if state in TERMINAL_STATES and self.finished_at is None:
            self.finished_at = self.updated_at

    def to_create_response(self, created: bool = True) -> dict[str, object]:
        return {
            "ok": True,
            "task_id": self.task_id,
            "request_id": self.request_id,
            "state": self.state,
        }

    def to_response(self) -> dict[str, object]:
        response: dict[str, object] = {
            "ok": self.state == "finished",
            "task_id": self.task_id,
            "request_id": self.request_id,
            "state": self.state,
            "phase": self.phase,
            "phase_message": self.phase_message,
            "vehicle": self.vehicle.to_response(),
            "operation": self.operation,
            "warnings": list(self.warnings),
            "timing": {
                "created_at": isoformat(self.created_at),
                "started_at": isoformat(self.started_at) if self.started_at else None,
                "finished_at": isoformat(self.finished_at) if self.finished_at else None,
                "duration_sec": self.duration_sec,
            },
            "runner": {
                "mode": self.runner_mode,
            },
        }
        if self.duration_sec is not None:
            response["duration_sec"] = self.duration_sec
        if self.summary is not None:
            response["summary"] = self.summary
        if self.data:
            response["data"] = self.data
        if self.artifacts:
            response["artifacts"] = self.artifacts
        if self.error_code is not None:
            response["error_code"] = self.error_code
        if self.message is not None:
            response["message"] = self.message
        return response
