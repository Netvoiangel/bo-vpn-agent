from __future__ import annotations

import json
import socket
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .artifacts import ArtifactStore
from .models import Task, VpnCredentials


StateCallback = Callable[[str, str], None]


@dataclass(slots=True)
class RunnerResult:
    summary: str
    data: dict[str, object]
    artifacts: list[dict[str, object]] | None = None
    warnings: list[str] | None = None


class VpnRunner:
    mode = "base"

    def __init__(self, artifact_store: ArtifactStore) -> None:
        self.artifact_store = artifact_store

    def run(self, task: Task, vpn: VpnCredentials, set_state: StateCallback) -> RunnerResult:
        raise NotImplementedError


class DryRunRunner(VpnRunner):
    mode = "dry_run"

    def run(self, task: Task, vpn: VpnCredentials, set_state: StateCallback) -> RunnerResult:
        self._simulate_lifecycle(set_state)
        return self._operation_result(task)

    def _simulate_lifecycle(self, set_state: StateCallback) -> None:
        phases = (
            ("starting_vpn", "Подключение к UniVPN"),
            ("vpn_connected", "UniVPN подключён"),
            ("checking_vehicle", "Проверка доступности ТС"),
            ("running_operation", "Выполнение операции"),
            ("collecting_result", "Сбор результата"),
        )
        for state, message in phases:
            set_state(state, message)
            time.sleep(0.01)

    def _operation_result(self, task: Task) -> RunnerResult:
        if task.operation == "vehicle_reachability":
            return RunnerResult(
                summary="ТС доступно по проверяемым TCP-портам",
                data={"tcp": {"22": True, "443": True, "80": False}, "ping": None},
            )
        if task.operation == "basic_status":
            return RunnerResult(
                summary="ТС доступно, основные сервисы работают",
                data={
                    "hostname": "dry-run-vehicle",
                    "uptime": "2 days, 04:11",
                    "date": "2026-06-29T10:15:42Z",
                    "disk": {"root_used_percent": 41},
                    "memory": {"used_percent": 53},
                    "ssh_22": True,
                    "https_443": True,
                    "services": {"mnt-brd": "active", "mnt-rout": "active"},
                },
            )
        if task.operation == "validators_status":
            return RunnerResult(
                summary="Валидаторы отвечают, критичных ошибок не найдено",
                data={
                    "validators": [
                        {"id": "front", "reachable": True, "status": "ok"},
                        {"id": "rear", "reachable": True, "status": "ok"},
                    ],
                    "read_only": True,
                },
            )
        if task.operation == "collect_bundle_light":
            artifact = self.artifact_store.create_text_artifact(
                f"vehicle-{task.vehicle.number}-collect-bundle-light.txt",
                "dry-run diagnostic bundle\nsecrets: redacted\n",
            )
            return RunnerResult(
                summary="Лёгкий диагностический пакет собран",
                data={"bundle": "light", "read_only": True},
                artifacts=[artifact],
            )
        return RunnerResult(summary="Операция выполнена", data={})


class ExistingContainerRunner(VpnRunner):
    mode = "existing_container"

    def run(self, task: Task, vpn: VpnCredentials, set_state: StateCallback) -> RunnerResult:
        set_state("starting_vpn", "Использование существующего UniVPN namespace")
        set_state("vpn_connected", "Существующий UniVPN namespace выбран")
        set_state("checking_vehicle", "Проверка TCP-доступности ТС")
        tcp = {
            "22": _tcp_connect(task.vehicle.ip, 22, timeout=3),
            "443": _tcp_connect(task.vehicle.ip, 443, timeout=3),
            "80": _tcp_connect(task.vehicle.ip, 80, timeout=3),
        }
        if not any(tcp.values()):
            raise RunnerFailure("vehicle_unreachable", "ТС недоступно по проверяемым TCP-портам")
        set_state("running_operation", "Выполнение read-only операции")
        set_state("collecting_result", "Сбор результата")
        return RunnerResult(
            summary="ТС доступно из текущего сетевого окружения runner-а",
            data={"tcp": tcp, "runner_note": "existing_container mode expects execution inside VPN namespace"},
        )


class JobContainerRunner(VpnRunner):
    mode = "job_container"

    def run(self, task: Task, vpn: VpnCredentials, set_state: StateCallback) -> RunnerResult:
        set_state("starting_vpn", "Подготовка одноразового UniVPN job container")
        raise RunnerFailure(
            "vpn_client_error",
            "job_container runner требует установленный vpn runner-daemon на хосте",
        )


class RunnerDaemonClient(VpnRunner):
    mode = "runner_daemon"

    def __init__(self, runner_url: str, auth_token: str | None = None, poll_interval_sec: float = 0.2) -> None:
        self.runner_url = runner_url.rstrip("/")
        self.auth_token = auth_token
        self.poll_interval_sec = poll_interval_sec

    def run(self, task: Task, vpn: VpnCredentials, set_state: StateCallback) -> RunnerResult:
        job = self._create_job(task, vpn)
        job_id = str(job["job_id"])
        while True:
            state = str(job.get("state", "created"))
            phase_message = str(job.get("phase_message", state))
            if state in {"starting_vpn", "vpn_connected", "checking_vehicle", "running_operation", "collecting_result", "cleanup"}:
                set_state(state, phase_message)
            if state == "finished":
                return RunnerResult(
                    summary=str(job.get("summary", "Операция выполнена")),
                    data=_dict_or_empty(job.get("data")),
                    artifacts=_list_or_empty(job.get("artifacts")),
                    warnings=_list_or_empty(job.get("warnings")),
                )
            if state == "timeout":
                raise RunnerFailure(str(job.get("error_code") or "operation_timeout"), str(job.get("message") or "Операция превысила timeout"))
            if state == "failed":
                raise RunnerFailure(str(job.get("error_code") or "runner_failed"), str(job.get("message") or "Runner job failed"))
            time.sleep(self.poll_interval_sec)
            job = self._get_job(job_id)

    def _create_job(self, task: Task, vpn: VpnCredentials) -> dict[str, Any]:
        payload = {
            "request_id": task.request_id,
            "runner_mode": task.runner_mode,
            "vehicle": task.vehicle.to_response(),
            "vpn": {"mode": vpn.mode, "username": vpn.username, "password": vpn.password},
            "operation": task.operation,
            "params": task.params,
            "timeout_sec": task.timeout_sec,
        }
        return self._request("POST", "/jobs", payload)

    def _get_job(self, job_id: str) -> dict[str, Any]:
        return self._request("GET", f"/jobs/{job_id}", None)

    def _request(self, method: str, path: str, payload: dict[str, Any] | None) -> dict[str, Any]:
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"
        request = Request(f"{self.runner_url}{path}", data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=10) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            try:
                error = json.loads(body)
            except json.JSONDecodeError:
                error = {}
            raise RunnerFailure(str(error.get("error_code") or "runner_http_error"), str(error.get("message") or "Runner daemon HTTP error")) from exc
        except (OSError, URLError, TimeoutError) as exc:
            raise RunnerFailure("runner_unavailable", "Runner daemon недоступен") from exc


class RunnerFailure(Exception):
    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message


def create_runner(
    mode: str,
    artifact_store: ArtifactStore,
    runner_url: str | None = None,
    runner_auth_token: str | None = None,
) -> VpnRunner:
    if runner_url:
        return RunnerDaemonClient(runner_url, runner_auth_token)
    if mode == "dry_run":
        return DryRunRunner(artifact_store)
    if mode == "existing_container":
        return ExistingContainerRunner(artifact_store)
    if mode == "job_container":
        return JobContainerRunner(artifact_store)
    raise ValueError(f"unknown runner mode: {mode}")


def _tcp_connect(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _dict_or_empty(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _list_or_empty(value: object) -> list[object]:
    return value if isinstance(value, list) else []
