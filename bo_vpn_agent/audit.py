from __future__ import annotations

import json
from pathlib import Path

from .models import Task, isoformat, utc_now


class AuditLogger:
    def __init__(self, path: Path) -> None:
        self.path = path

    def write_task_event(self, task: Task) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": isoformat(utc_now()),
            "request_id": task.request_id,
            "task_id": task.task_id,
            "telegram_user_id": task.telegram_user_id,
            "user_role": task.user_role,
            "operation": task.operation,
            "vehicle_number": task.vehicle.number,
            "vehicle_ip": task.vehicle.ip,
            "result_state": task.state,
            "error_code": task.error_code,
            "duration_sec": task.duration_sec,
            "runner_mode": task.runner_mode,
            "risk_level": task.risk,
            "state_changing": task.state_changing,
            "cleanup_failed": task.cleanup_failed,
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
