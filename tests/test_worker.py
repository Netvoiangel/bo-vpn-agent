from __future__ import annotations

import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from bo_vpn_agent.api import validate_service_headers
from bo_vpn_agent.config import WorkerConfig
from bo_vpn_agent.service import WorkerService


def make_config(tmp_path: Path, runner_mode: str = "dry_run") -> WorkerConfig:
    return WorkerConfig(
        service_token="test-token",
        runner_mode=runner_mode,
        audit_log_path=tmp_path / "audit.log",
        artifact_dir=tmp_path / "artifacts",
        global_create_limit_per_minute=100,
        user_create_limit_per_minute=100,
    )


def task_payload(request_id: str = "req-1", operation: str = "basic_status") -> dict[str, object]:
    return {
        "request_id": request_id,
        "telegram_user_id": 123456,
        "user_role": "engineer",
        "vehicle": {"number": "6968", "ip": "172.26.128.11"},
        "vpn": {"mode": "inline_once", "username": "secret-user", "password": "secret-password"},
        "operation": operation,
        "params": {},
        "timeout_sec": 5,
    }


def wait_for_terminal(service: WorkerService, task_id: str) -> dict[str, object]:
    for _ in range(200):
        response = service.get_task(task_id)
        if response["state"] in {"finished", "failed", "timeout"}:
            return response
        time.sleep(0.01)
    raise AssertionError("task did not finish")


class WorkerServiceTests(unittest.TestCase):
    def test_capabilities_only_contains_mvp_operations(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            service = WorkerService(make_config(Path(raw_tmp)))

            names = {operation["name"] for operation in service.capabilities()["operations"]}

            self.assertEqual(names, {"vehicle_reachability", "basic_status", "validators_status", "collect_bundle_light"})
            self.assertNotIn("ui_screenshot", names)
            self.assertNotIn("run_command", names)
            self.assertNotIn("select_route", names)

    def test_create_task_idempotency_and_secret_cleanup(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            service = WorkerService(make_config(Path(raw_tmp)))
            payload = task_payload()

            status, created = service.create_task(payload)
            duplicate_status, duplicate = service.create_task(payload)
            result = wait_for_terminal(service, created["task_id"])

            self.assertEqual(status, 201)
            self.assertEqual(duplicate_status, 200)
            self.assertEqual(duplicate["task_id"], created["task_id"])
            self.assertEqual(result["state"], "finished")
            self.assertIs(result["ok"], True)
            self.assertNotIn("secret-password", json.dumps(result, ensure_ascii=False))
            self.assertEqual(service.secrets_by_task_id, {})

    def test_request_id_conflict(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            service = WorkerService(make_config(Path(raw_tmp)))
            payload = task_payload()
            status, _ = service.create_task(payload)
            changed = task_payload()
            changed["operation"] = "vehicle_reachability"

            with self.assertRaises(Exception) as raised:
                service.create_task(changed)

            self.assertEqual(getattr(raised.exception, "status"), 409)
            self.assertEqual(getattr(raised.exception, "error_code"), "request_id_conflict")
            self.assertEqual(status, 201)

    def test_worker_busy_for_different_request_while_active(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            service = WorkerService(make_config(Path(raw_tmp), runner_mode="job_container"))
            first_status, first = service.create_task(task_payload("req-1"))

            with self.assertRaises(Exception) as raised:
                service.create_task(task_payload("req-2"))

            self.assertEqual(getattr(raised.exception, "status"), 409)
            self.assertEqual(getattr(raised.exception, "error_code"), "worker_busy")

            result = wait_for_terminal(service, first["task_id"])
            self.assertEqual(first_status, 201)
            self.assertEqual(result["state"], "failed")

    def test_audit_log_has_no_vpn_secrets(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            tmp_path = Path(raw_tmp)
            service = WorkerService(make_config(tmp_path))
            _, created = service.create_task(task_payload(operation="collect_bundle_light"))
            wait_for_terminal(service, created["task_id"])

            audit_text = (tmp_path / "audit.log").read_text(encoding="utf-8")

            self.assertNotIn("secret-password", audit_text)
            self.assertNotIn("secret-user", audit_text)
            self.assertIn("collect_bundle_light", audit_text)

    def test_service_auth_headers(self) -> None:
        validate_service_headers({"Authorization": "Bearer test-token", "X-Request-Id": "http-test"}, "test-token")

        with self.assertRaises(Exception) as no_auth:
            validate_service_headers({}, "test-token")
        self.assertEqual(getattr(no_auth.exception, "status"), 401)
        self.assertEqual(getattr(no_auth.exception, "error_code"), "unauthorized")

        with self.assertRaises(Exception) as no_request_id:
            validate_service_headers({"Authorization": "Bearer test-token"}, "test-token")
        self.assertEqual(getattr(no_request_id.exception, "status"), 400)
        self.assertEqual(getattr(no_request_id.exception, "error_code"), "invalid_request")


if __name__ == "__main__":
    unittest.main()
