from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class WorkerConfig:
    service_token: str
    runner_mode: str = "dry_run"
    host: str = "127.0.0.1"
    port: int = 8080
    audit_log_path: Path = Path("var/audit.log")
    artifact_dir: Path = Path("var/artifacts")
    artifact_ttl_hours: int = 24
    max_artifact_bytes: int = 10 * 1024 * 1024
    task_timeout_sec_default: int = 120
    task_result_ttl_sec: int = 24 * 60 * 60
    global_create_limit_per_minute: int = 60
    user_create_limit_per_minute: int = 10

    @classmethod
    def from_env(cls) -> "WorkerConfig":
        return cls(
            service_token=os.getenv("BOT_TO_WORKER_SERVICE_TOKEN", "dev-token"),
            runner_mode=os.getenv("BO_VPN_RUNNER_MODE", "dry_run"),
            host=os.getenv("BO_VPN_WORKER_HOST", "127.0.0.1"),
            port=int(os.getenv("BO_VPN_WORKER_PORT", "8080")),
            audit_log_path=Path(os.getenv("BO_VPN_AUDIT_LOG", "var/audit.log")),
            artifact_dir=Path(os.getenv("BO_VPN_ARTIFACT_DIR", "var/artifacts")),
            artifact_ttl_hours=int(os.getenv("BO_VPN_ARTIFACT_TTL_HOURS", "24")),
            max_artifact_bytes=int(os.getenv("BO_VPN_MAX_ARTIFACT_BYTES", str(10 * 1024 * 1024))),
            task_timeout_sec_default=int(os.getenv("BO_VPN_TASK_TIMEOUT_SEC", "120")),
            global_create_limit_per_minute=int(os.getenv("BO_VPN_GLOBAL_CREATE_LIMIT_PER_MINUTE", "60")),
            user_create_limit_per_minute=int(os.getenv("BO_VPN_USER_CREATE_LIMIT_PER_MINUTE", "10")),
        )
