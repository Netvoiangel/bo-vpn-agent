from __future__ import annotations

import socket
import time
from dataclasses import dataclass
from typing import Callable

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


class RunnerFailure(Exception):
    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message


def create_runner(mode: str, artifact_store: ArtifactStore) -> VpnRunner:
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
