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
    vehicle_inventory_path: Path | None = None
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
            vehicle_inventory_path=Path(os.environ["BO_VPN_VEHICLE_INVENTORY_PATH"])
            if os.getenv("BO_VPN_VEHICLE_INVENTORY_PATH")
            else None,
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
    existing_container_name: str = "univpn-service"
    nsenter_bin: str = "/usr/bin/nsenter"
    docker_bin: str = "/usr/bin/docker"
    ssh_bin: str = "/usr/bin/ssh"
    ssh_key_path: Path = Path("/home/timur/univpn/rsa.key")
    default_ssh_user: str = "root"
    nsenter_timeout_sec: int = 8
    command_output_max_bytes: int = 64 * 1024
    artifact_dir: Path = Path("var/runner-artifacts")
    artifact_ttl_hours: int = 24
    max_artifact_bytes: int = 10 * 1024 * 1024

    @classmethod
    def from_env(cls) -> "RunnerDaemonConfig":
        return cls(
            host=os.getenv("BO_VPN_RUNNER_HOST", "127.0.0.1"),
            port=int(os.getenv("BO_VPN_RUNNER_PORT", "8091")),
            auth_token=os.getenv("BO_VPN_RUNNER_AUTH_TOKEN"),
            existing_container_name=os.getenv("BO_VPN_EXISTING_CONTAINER_NAME", "univpn-service"),
            nsenter_bin=os.getenv("BO_VPN_NSENTER_BIN", "/usr/bin/nsenter"),
            docker_bin=os.getenv("BO_VPN_DOCKER_BIN", "/usr/bin/docker"),
            ssh_bin=os.getenv("BO_VPN_SSH_BIN", "/usr/bin/ssh"),
            ssh_key_path=Path(os.getenv("BO_VPN_SSH_KEY_PATH", "/home/timur/univpn/rsa.key")),
            default_ssh_user=os.getenv("BO_VPN_DEFAULT_SSH_USER", "root"),
            nsenter_timeout_sec=int(os.getenv("BO_VPN_NSENTER_TIMEOUT_SEC", "8")),
            command_output_max_bytes=int(os.getenv("BO_VPN_COMMAND_OUTPUT_MAX_BYTES", str(64 * 1024))),
            artifact_dir=Path(os.getenv("BO_VPN_RUNNER_ARTIFACT_DIR", "var/runner-artifacts")),
            artifact_ttl_hours=int(os.getenv("BO_VPN_ARTIFACT_TTL_HOURS", "24")),
            max_artifact_bytes=int(os.getenv("BO_VPN_MAX_ARTIFACT_BYTES", str(10 * 1024 * 1024))),
        )
