from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Mapping
from urllib.parse import urlparse

from .config import WorkerConfig
from .errors import WorkerError
from .security import check_bearer
from .service import WorkerService, load_json_body


def validate_service_headers(headers: Mapping[str, str | None], service_token: str) -> None:
    if not check_bearer(headers.get("Authorization"), service_token):
        raise WorkerError(401, "unauthorized", "Service auth отсутствует или некорректна")
    if not headers.get("X-Request-Id"):
        raise WorkerError(400, "invalid_request", "X-Request-Id обязателен")


def make_handler(service: WorkerService) -> type[BaseHTTPRequestHandler]:
    class WorkerHandler(BaseHTTPRequestHandler):
        server_version = "bo-vpn-worker/0.1"

        def log_message(self, format: str, *args: object) -> None:
            # Keep default access logs from accidentally growing with request bodies.
            return

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            try:
                if path == "/health":
                    self._send_json(HTTPStatus.OK, service.health())
                    return
                self._require_service_auth()
                if path == "/capabilities":
                    self._send_json(HTTPStatus.OK, service.capabilities())
                    return
                if path.startswith("/tasks/"):
                    task_id = path.removeprefix("/tasks/")
                    self._send_json(HTTPStatus.OK, service.get_task(task_id))
                    return
                raise WorkerError(404, "not_found", "Endpoint не найден")
            except WorkerError as exc:
                self._send_json(exc.status, exc.to_response())

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            try:
                self._require_service_auth()
                if path != "/tasks":
                    raise WorkerError(404, "not_found", "Endpoint не найден")
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0:
                    raise WorkerError(400, "invalid_request", "Пустое тело запроса")
                if length > 1024 * 1024:
                    raise WorkerError(400, "invalid_request", "Тело запроса слишком большое")
                payload = load_json_body(self.rfile.read(length))
                status, response = service.create_task(payload)
                self._send_json(status, response)
            except WorkerError as exc:
                self._send_json(exc.status, exc.to_response())

        def _require_service_auth(self) -> None:
            validate_service_headers(self.headers, service.config.service_token)

        def _send_json(self, status: int, payload: dict[str, object]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return WorkerHandler


def run_server(config: WorkerConfig) -> None:
    service = WorkerService(config)
    server = ThreadingHTTPServer((config.host, config.port), make_handler(service))
    print(f"bo-vpn-worker listening on http://{config.host}:{config.port}")
    server.serve_forever()
