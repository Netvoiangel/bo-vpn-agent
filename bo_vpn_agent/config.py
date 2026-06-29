from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class WorkerConfig:
    service_token: str
    runner_mode: str = "dry_run"
    runner_url: str | None = None
    runner_auth_token: str | None = None
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
        service_token = os.getenv("BO_VPN_WORKER_AUTH_TOKEN") or os.getenv("BOT_TO_WORKER_SERVICE_TOKEN", "dev-token")
        runner_mode = os.getenv("BO_VPN_DEFAULT_RUNNER_MODE") or os.getenv("BO_VPN_RUNNER_MODE", "dry_run")
        audit_log_path = os.getenv("BO_VPN_AUDIT_LOG_PATH") or os.getenv("BO_VPN_AUDIT_LOG", "var/audit.log")
        return cls(
            service_token=service_token,
            runner_mode=runner_mode,
            runner_url=os.getenv("BO_VPN_RUNNER_URL"),
            runner_auth_token=os.getenv("BO_VPN_RUNNER_AUTH_TOKEN"),
            host=os.getenv("BO_VPN_WORKER_HOST", "127.0.0.1"),
            port=int(os.getenv("BO_VPN_WORKER_PORT", "8080")),
            audit_log_path=Path(audit_log_path),
            artifact_dir=Path(os.getenv("BO_VPN_ARTIFACT_DIR", "var/artifacts")),
            artifact_ttl_hours=int(os.getenv("BO_VPN_ARTIFACT_TTL_HOURS", "24")),
            max_artifact_bytes=int(os.getenv("BO_VPN_MAX_ARTIFACT_BYTES", str(10 * 1024 * 1024))),
            task_timeout_sec_default=int(os.getenv("BO_VPN_TASK_TIMEOUT_SEC", "120")),
            global_create_limit_per_minute=int(os.getenv("BO_VPN_GLOBAL_CREATE_LIMIT_PER_MINUTE", "60")),
            user_create_limit_per_minute=int(os.getenv("BO_VPN_USER_CREATE_LIMIT_PER_MINUTE", "10")),
        )


@dataclass(slots=True)
class RunnerDaemonConfig:
    host: str = "127.0.0.1"
    port: int = 8091
    auth_token: str | None = None
    artifact_dir: Path = Path("var/runner-artifacts")
    artifact_ttl_hours: int = 24
    max_artifact_bytes: int = 10 * 1024 * 1024

    @classmethod
    def from_env(cls) -> "RunnerDaemonConfig":
        return cls(
            host=os.getenv("BO_VPN_RUNNER_HOST", "127.0.0.1"),
            port=int(os.getenv("BO_VPN_RUNNER_PORT", "8091")),
            auth_token=os.getenv("BO_VPN_RUNNER_AUTH_TOKEN"),
            artifact_dir=Path(os.getenv("BO_VPN_RUNNER_ARTIFACT_DIR", "var/runner-artifacts")),
            artifact_ttl_hours=int(os.getenv("BO_VPN_ARTIFACT_TTL_HOURS", "24")),
            max_artifact_bytes=int(os.getenv("BO_VPN_MAX_ARTIFACT_BYTES", str(10 * 1024 * 1024))),
        )
