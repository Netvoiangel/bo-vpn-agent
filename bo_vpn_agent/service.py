from __future__ import annotations

import json
import threading
import time
import uuid
from collections import defaultdict, deque
from typing import Any

from .artifacts import ArtifactStore
from .audit import AuditLogger
from .config import WorkerConfig
from .errors import ValidationError, WorkerError
from .models import TERMINAL_STATES, Task, Vehicle, VpnCredentials
from .operations import capabilities_response, get_operation
from .runner import RunnerFailure, create_runner
from .security import fingerprint_request
from .vehicle_inventory import VehicleInventory


class RateLimiter:
    def __init__(self, global_limit: int, user_limit: int) -> None:
        self.global_limit = global_limit
        self.user_limit = user_limit
        self.global_events: deque[float] = deque()
        self.user_events: dict[int, deque[float]] = defaultdict(deque)

    def check(self, telegram_user_id: int) -> bool:
        now = time.monotonic()
        cutoff = now - 60
        self._trim(self.global_events, cutoff)
        user_queue = self.user_events[telegram_user_id]
        self._trim(user_queue, cutoff)
        if len(self.global_events) >= self.global_limit or len(user_queue) >= self.user_limit:
            return False
        self.global_events.append(now)
        user_queue.append(now)
        return True

    @staticmethod
    def _trim(events: deque[float], cutoff: float) -> None:
        while events and events[0] < cutoff:
            events.popleft()


class WorkerService:
    def __init__(self, config: WorkerConfig) -> None:
        self.config = config
        self.artifact_store = ArtifactStore(config.artifact_dir, config.artifact_ttl_hours, config.max_artifact_bytes)
        self.audit = AuditLogger(config.audit_log_path)
        self.vehicle_inventory = VehicleInventory(config.vehicle_inventory_path) if config.vehicle_inventory_path else None
        self.rate_limiter = RateLimiter(config.global_create_limit_per_minute, config.user_create_limit_per_minute)
        self.lock = threading.RLock()
        self.tasks_by_id: dict[str, Task] = {}
        self.tasks_by_request_id: dict[str, Task] = {}
        self.secrets_by_task_id: dict[str, VpnCredentials] = {}

    def health(self) -> dict[str, object]:
        return {"ok": True, "worker": "bo-vpn-worker", "status": "busy" if self._active_task() else "idle"}

    def capabilities(self) -> dict[str, object]:
        return capabilities_response()

    def create_task(self, payload: dict[str, Any]) -> tuple[int, dict[str, object]]:
        request_id = self._require_str(payload, "request_id")
        fingerprint = fingerprint_request(payload)
        with self.lock:
            existing = self.tasks_by_request_id.get(request_id)
            if existing is not None:
                if existing.request_fingerprint != fingerprint:
                    raise WorkerError(409, "request_id_conflict", "request_id уже использован с другим телом запроса")
                return 200, existing.to_create_response(created=False)

            telegram_user_id = self._require_int(payload, "telegram_user_id")
            if not self.rate_limiter.check(telegram_user_id):
                raise WorkerError(429, "rate_limited", "Превышен лимит создания задач")

            active = self._active_task()
            if active is not None:
                raise WorkerError(409, "worker_busy", "Worker уже выполняет задачу")

            task, vpn = self._build_task(payload, fingerprint)
            self.tasks_by_id[task.task_id] = task
            self.tasks_by_request_id[task.request_id] = task
            self.secrets_by_task_id[task.task_id] = vpn
            response = task.to_create_response(created=True)
            thread = threading.Thread(target=self._execute_task, args=(task.task_id,), daemon=True)
            thread.start()
            return 201, response

    def get_task(self, task_id: str) -> dict[str, object]:
        with self.lock:
            task = self.tasks_by_id.get(task_id)
            if task is None:
                raise WorkerError(404, "task_not_found", "Задача не найдена")
            return task.to_response()

    def resolve_vehicle(self, query: str) -> dict[str, object]:
        if self.vehicle_inventory is None:
            raise WorkerError(422, "vehicle_ip_not_found", "Vehicle inventory is not configured")
        record = self.vehicle_inventory.resolve_query(query)
        return {"ok": True, "query": query, "vehicle": record.to_response()}

    def _build_task(self, payload: dict[str, Any], fingerprint: str) -> tuple[Task, VpnCredentials]:
        vehicle_raw = payload.get("vehicle")
        if not isinstance(vehicle_raw, dict):
            raise ValidationError("vehicle обязателен")
        vehicle = self._build_vehicle(vehicle_raw)

        vpn_raw = payload.get("vpn")
        if not isinstance(vpn_raw, dict):
            raise ValidationError("vpn обязателен")
        vpn_mode = self._require_str(vpn_raw, "mode", "vpn.mode")
        if vpn_mode == "inline_once":
            vpn = VpnCredentials(
                mode=vpn_mode,
                username=self._require_str(vpn_raw, "username", "vpn.username"),
                password=self._require_str(vpn_raw, "password", "vpn.password"),
            )
        elif vpn_mode == "container_secret":
            vpn = VpnCredentials(mode=vpn_mode, username="", password="")
        else:
            raise ValidationError("В MVP поддерживается только vpn.mode=inline_once или container_secret", "invalid_request", 400)

        operation_name = self._require_str(payload, "operation")
        operation = get_operation(operation_name)
        user_role = str(payload.get("user_role", payload.get("role", "engineer")))
        if user_role not in operation.roles_allowed:
            raise WorkerError(403, "operation_not_allowed", "Операция запрещена для роли пользователя")

        params = payload.get("params", {})
        if not isinstance(params, dict):
            raise ValidationError("params должен быть объектом")
        timeout_sec = int(payload.get("timeout_sec", operation.timeout_sec_default))
        if timeout_sec <= 0:
            raise ValidationError("timeout_sec должен быть положительным")

        runner_mode = str(payload.get("runner_mode", self.config.runner_mode))
        task = Task(
            task_id=str(uuid.uuid4()),
            request_id=self._require_str(payload, "request_id"),
            telegram_user_id=self._require_int(payload, "telegram_user_id"),
            user_role=user_role,
            vehicle=vehicle,
            operation=operation.name,
            params=params,
            timeout_sec=timeout_sec,
            request_fingerprint=fingerprint,
            runner_mode=runner_mode,
            risk=operation.risk,
            state_changing=operation.state_changing,
        )
        return task, vpn

    def _build_vehicle(self, vehicle_raw: dict[str, Any]) -> Vehicle:
        ip = vehicle_raw.get("ip")
        if isinstance(ip, str) and ip.strip():
            return Vehicle(
                number=self._require_str(vehicle_raw, "number", "vehicle.number"),
                ip=ip.strip(),
            )
        if self.vehicle_inventory is None:
            raise WorkerError(422, "vehicle_ip_not_found", "Vehicle inventory is not configured")
        record = self.vehicle_inventory.resolve_vehicle(vehicle_raw)
        return Vehicle(number=record.number, ip=record.ip)

    def _execute_task(self, task_id: str) -> None:
        vpn: VpnCredentials | None = None
        try:
            with self.lock:
                task = self.tasks_by_id[task_id]
                vpn = self.secrets_by_task_id.get(task_id)
            if vpn is None:
                raise RunnerFailure("vpn_client_error", "VPN credentials отсутствуют")
            runner = create_runner(task.runner_mode, self.artifact_store, self.config.runner_url, self.config.runner_auth_token)

            result_holder: dict[str, Any] = {}
            failure_holder: dict[str, Any] = {}

            def run_runner() -> None:
                try:
                    result_holder["result"] = runner.run(task, vpn, lambda state, msg: self._set_state(task_id, state, msg))
                except BaseException as exc:  # noqa: BLE001 - copied into main worker thread
                    failure_holder["error"] = exc

            runner_thread = threading.Thread(target=run_runner, daemon=True)
            runner_thread.start()
            runner_thread.join(task.timeout_sec)
            if runner_thread.is_alive():
                with self.lock:
                    task = self.tasks_by_id[task_id]
                    task.error_code = "operation_timeout"
                    task.message = "Операция превысила timeout"
                    task.set_state("cleanup", "Очистка временного состояния после timeout")
                    task.set_state("timeout", "Операция превысила timeout")
                return

            if "error" in failure_holder:
                error = failure_holder["error"]
                if isinstance(error, RunnerFailure):
                    with self.lock:
                        task = self.tasks_by_id[task_id]
                        task.error_code = error.error_code
                        task.message = error.message
                        task.set_state("cleanup", "Очистка временного состояния после ошибки")
                        task.set_state("failed", error.message)
                    return
                raise error

            result = result_holder["result"]
            with self.lock:
                task = self.tasks_by_id[task_id]
                task.summary = result.summary
                task.data = result.data
                task.artifacts = result.artifacts or []
                task.warnings.extend(result.warnings or [])
                task.set_state("cleanup", "Очистка временного состояния")
                task.set_state("finished", "Диагностика завершена")
        except BaseException as exc:  # noqa: BLE001 - keep worker alive and normalize errors
            with self.lock:
                task = self.tasks_by_id[task_id]
                task.error_code = "internal_error"
                task.message = str(exc)
                task.set_state("cleanup", "Очистка временного состояния после ошибки")
                task.set_state("failed", "Внутренняя ошибка worker-а")
        finally:
            with self.lock:
                task = self.tasks_by_id[task_id]
                self.secrets_by_task_id.pop(task_id, None)
                if task.state not in TERMINAL_STATES:
                    task.set_state("cleanup", "Очистка временного состояния")
                    task.set_state("failed", "Задача завершилась без результата")
                self.audit.write_task_event(task)

    def _set_state(self, task_id: str, state: str, message: str) -> None:
        with self.lock:
            task = self.tasks_by_id[task_id]
            if task.state in TERMINAL_STATES:
                return
            task.set_state(state, message)

    def _active_task(self) -> Task | None:
        for task in self.tasks_by_id.values():
            if task.state not in TERMINAL_STATES:
                return task
        return None

    @staticmethod
    def _require_str(payload: dict[str, Any], key: str, label: str | None = None) -> str:
        value = payload.get(key)
        if not isinstance(value, str) or not value:
            raise ValidationError(f"{label or key} обязателен")
        return value

    @staticmethod
    def _require_int(payload: dict[str, Any], key: str) -> int:
        value = payload.get(key)
        if not isinstance(value, int):
            raise ValidationError(f"{key} обязателен")
        return value


def load_json_body(raw_body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError("Некорректный JSON") from exc
    if not isinstance(payload, dict):
        raise ValidationError("Тело запроса должно быть JSON-объектом")
    return payload
