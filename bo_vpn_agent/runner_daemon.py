from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping
from urllib.parse import urlparse

from .artifacts import ArtifactStore
from .config import RunnerDaemonConfig
from .errors import ValidationError, WorkerError
from .models import TASK_STATES, TERMINAL_STATES, Task, Vehicle, VpnCredentials
from .runner import RunnerFailure, create_runner
from .security import check_bearer
from .service import load_json_body


@dataclass(slots=True)
class RunnerJob:
    job_id: str
    request_id: str
    task: Task
    vpn: VpnCredentials
    state: str = "created"
    phase_message: str = "Job создан"
    summary: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error_code: str | None = None
    message: str | None = None

    def set_state(self, state: str, message: str) -> None:
        if state not in TASK_STATES and state != "cancelled":
            raise ValueError(f"unknown job state: {state}")
        self.state = state
        self.phase_message = message

    def to_response(self) -> dict[str, object]:
        response: dict[str, object] = {
            "ok": self.state == "finished",
            "job_id": self.job_id,
            "request_id": self.request_id,
            "state": self.state,
            "phase_message": self.phase_message,
            "operation": self.task.operation,
            "warnings": list(self.warnings),
        }
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


class RunnerDaemonService:
    def __init__(self, config: RunnerDaemonConfig) -> None:
        self.config = config
        self.artifact_store = ArtifactStore(config.artifact_dir, config.artifact_ttl_hours, config.max_artifact_bytes)
        self.jobs: dict[str, RunnerJob] = {}
        self.lock = threading.RLock()

    def health(self) -> dict[str, object]:
        return {"ok": True, "runner": "bo-vpn-runner-daemon", "status": "ready"}

    def create_job(self, payload: dict[str, Any]) -> tuple[int, dict[str, object]]:
        job = self._build_job(payload)
        with self.lock:
            self.jobs[job.job_id] = job
        thread = threading.Thread(target=self._execute_job, args=(job.job_id,), daemon=True)
        thread.start()
        return 201, job.to_response()

    def get_job(self, job_id: str) -> dict[str, object]:
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                raise WorkerError(404, "job_not_found", "Job не найден")
            return job.to_response()

    def cancel_job(self, job_id: str) -> dict[str, object]:
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                raise WorkerError(404, "job_not_found", "Job не найден")
            if job.state not in TERMINAL_STATES:
                job.error_code = "operation_cancelled"
                job.message = "Job отменён"
                job.set_state("failed", "Job отменён")
            return job.to_response()

    def _build_job(self, payload: dict[str, Any]) -> RunnerJob:
        request_id = _require_str(payload, "request_id")
        runner_mode = _require_str(payload, "runner_mode")
        vehicle_raw = payload.get("vehicle")
        vpn_raw = payload.get("vpn")
        if not isinstance(vehicle_raw, dict):
            raise ValidationError("vehicle обязателен")
        if not isinstance(vpn_raw, dict):
            raise ValidationError("vpn обязателен")
        vehicle = Vehicle(number=_require_str(vehicle_raw, "number", "vehicle.number"), ip=_require_str(vehicle_raw, "ip", "vehicle.ip"))
        vpn = VpnCredentials(
            mode=_require_str(vpn_raw, "mode", "vpn.mode"),
            username=_require_str(vpn_raw, "username", "vpn.username"),
            password=_require_str(vpn_raw, "password", "vpn.password"),
        )
        task = Task(
            task_id=str(uuid.uuid4()),
            request_id=request_id,
            telegram_user_id=0,
            user_role="runner-daemon",
            vehicle=vehicle,
            operation=_require_str(payload, "operation"),
            params=payload.get("params") if isinstance(payload.get("params"), dict) else {},
            timeout_sec=int(payload.get("timeout_sec", 120)),
            request_fingerprint="runner-daemon",
            runner_mode=runner_mode,
            risk="read_only",
        )
        return RunnerJob(job_id=str(uuid.uuid4()), request_id=request_id, task=task, vpn=vpn)

    def _execute_job(self, job_id: str) -> None:
        with self.lock:
            job = self.jobs[job_id]
        runner = create_runner(job.task.runner_mode, self.artifact_store)

        def set_state(state: str, message: str) -> None:
            with self.lock:
                current = self.jobs[job_id]
                if current.state in TERMINAL_STATES:
                    return
                current.set_state(state, message)

        try:
            result_holder: dict[str, Any] = {}
            failure_holder: dict[str, BaseException] = {}

            def run_runner() -> None:
                try:
                    result_holder["result"] = runner.run(job.task, job.vpn, set_state)
                except BaseException as exc:  # noqa: BLE001 - normalize below
                    failure_holder["error"] = exc

            thread = threading.Thread(target=run_runner, daemon=True)
            thread.start()
            thread.join(job.task.timeout_sec)
            with self.lock:
                current = self.jobs[job_id]
                if current.state in TERMINAL_STATES:
                    return
                if thread.is_alive():
                    current.error_code = "operation_timeout"
                    current.message = "Runner job превысил timeout"
                    current.set_state("timeout", "Runner job превысил timeout")
                    return
                if "error" in failure_holder:
                    error = failure_holder["error"]
                    if isinstance(error, RunnerFailure):
                        current.error_code = error.error_code
                        current.message = error.message
                    else:
                        current.error_code = "runner_internal_error"
                        current.message = str(error)
                    current.set_state("failed", current.message)
                    return
                result = result_holder["result"]
                current.summary = result.summary
                current.data = result.data
                current.artifacts = result.artifacts or []
                current.warnings = result.warnings or []
                current.set_state("finished", "Runner job завершён")
        finally:
            with self.lock:
                self.jobs[job_id].vpn = VpnCredentials(mode="inline_once", username="", password="")


def validate_runner_headers(headers: Mapping[str, str | None], auth_token: str | None) -> None:
    if auth_token and not check_bearer(headers.get("Authorization"), auth_token):
        raise WorkerError(401, "unauthorized", "Runner auth отсутствует или некорректна")


def make_runner_handler(service: RunnerDaemonService) -> type[BaseHTTPRequestHandler]:
    class RunnerHandler(BaseHTTPRequestHandler):
        server_version = "bo-vpn-runner-daemon/0.1"

        def log_message(self, format: str, *args: object) -> None:
            return

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            try:
                if path == "/health":
                    self._send_json(HTTPStatus.OK, service.health())
                    return
                validate_runner_headers(self.headers, service.config.auth_token)
                if path.startswith("/jobs/"):
                    self._send_json(HTTPStatus.OK, service.get_job(path.removeprefix("/jobs/")))
                    return
                raise WorkerError(404, "not_found", "Endpoint не найден")
            except WorkerError as exc:
                self._send_json(exc.status, exc.to_response())

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            try:
                validate_runner_headers(self.headers, service.config.auth_token)
                if path == "/jobs":
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0:
                        raise WorkerError(400, "invalid_request", "Пустое тело запроса")
                    payload = load_json_body(self.rfile.read(length))
                    status, response = service.create_job(payload)
                    self._send_json(status, response)
                    return
                if path.startswith("/jobs/") and path.endswith("/cancel"):
                    job_id = path.removeprefix("/jobs/").removesuffix("/cancel")
                    self._send_json(HTTPStatus.OK, service.cancel_job(job_id))
                    return
                raise WorkerError(404, "not_found", "Endpoint не найден")
            except WorkerError as exc:
                self._send_json(exc.status, exc.to_response())

        def _send_json(self, status: int, payload: dict[str, object]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return RunnerHandler


def run_runner_daemon(config: RunnerDaemonConfig) -> None:
    service = RunnerDaemonService(config)
    server = ThreadingHTTPServer((config.host, config.port), make_runner_handler(service))
    print(f"bo-vpn-runner-daemon listening on http://{config.host}:{config.port}")
    server.serve_forever()


def _require_str(payload: dict[str, Any], key: str, label: str | None = None) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{label or key} обязателен")
    return value
