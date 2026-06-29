from __future__ import annotations

import json
import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from bo_vpn_agent.api import validate_service_headers
from bo_vpn_agent.config import RunnerDaemonConfig, WorkerConfig
from bo_vpn_agent.runner_daemon import RunnerDaemonService
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
    def test_worker_config_reads_current_env_names(self) -> None:
        keys = {
            "BO_VPN_WORKER_AUTH_TOKEN": "token-from-env",
            "BO_VPN_DEFAULT_RUNNER_MODE": "existing_container",
            "BO_VPN_RUNNER_URL": "http://127.0.0.1:8091",
            "BO_VPN_AUDIT_LOG_PATH": "logs/audit.jsonl",
        }
        previous = {key: os.environ.get(key) for key in keys}
        try:
            os.environ.update(keys)
            config = WorkerConfig.from_env()
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.assertEqual(config.service_token, "token-from-env")
        self.assertEqual(config.runner_mode, "existing_container")
        self.assertEqual(config.runner_url, "http://127.0.0.1:8091")
        self.assertEqual(config.audit_log_path, Path("logs/audit.jsonl"))

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

    def test_task_response_contract(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            service = WorkerService(make_config(Path(raw_tmp)))

            status, created = service.create_task(task_payload(operation="vehicle_reachability"))
            result = wait_for_terminal(service, created["task_id"])

            self.assertEqual(status, 201)
            self.assertEqual(set(created), {"ok", "task_id", "request_id", "state"})
            self.assertIs(created["ok"], True)
            self.assertEqual(created["request_id"], "req-1")
            self.assertEqual(created["state"], "created")

            for field in ("ok", "task_id", "request_id", "state", "operation", "summary", "data", "warnings", "duration_sec"):
                self.assertIn(field, result)
            self.assertIs(result["ok"], True)
            self.assertEqual(result["state"], "finished")
            self.assertEqual(result["operation"], "vehicle_reachability")
            self.assertIsInstance(result["data"], dict)
            self.assertIsInstance(result["warnings"], list)
            self.assertIsInstance(result["duration_sec"], int)

    def test_job_container_returns_normalized_error_until_daemon_connected(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            service = WorkerService(make_config(Path(raw_tmp)))
            payload = task_payload("job-container-placeholder-test", "vehicle_reachability")
            payload["runner_mode"] = "job_container"

            status, created = service.create_task(payload)
            result = wait_for_terminal(service, created["task_id"])

            self.assertEqual(status, 201)
            self.assertEqual(result["state"], "failed")
            self.assertEqual(result["error_code"], "vpn_client_error")
            self.assertNotIn("secret-password", json.dumps(result, ensure_ascii=False))

    def test_error_response_redacts_validation_request_secrets(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            service = WorkerService(make_config(Path(raw_tmp)))
            payload = task_payload("bad-vpn-mode")
            payload["vpn"] = {
                "mode": "stored_ref",
                "username": "real.user@example.com",
                "password": "RealPassword123",
            }

            with self.assertRaises(Exception) as raised:
                service.create_task(payload)

            body = json.dumps(raised.exception.to_response(), ensure_ascii=False)
            self.assertEqual(getattr(raised.exception, "status"), 400)
            self.assertNotIn("RealPassword123", body)
            self.assertNotIn("real.user@example.com", body)
            self.assertNotIn("RealPassword123", str(raised.exception))
            self.assertFalse((Path(raw_tmp) / "audit.log").exists())

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

    def test_runner_daemon_skeleton_contract(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            service = RunnerDaemonService(
                RunnerDaemonConfig(
                    artifact_dir=Path(raw_tmp) / "runner-artifacts",
                    artifact_ttl_hours=24,
                )
            )
            status, created = service.create_job(
                {
                    "request_id": "runner-job-1",
                    "runner_mode": "dry_run",
                    "vehicle": {"number": "6968", "ip": "172.26.130.165"},
                    "vpn": {"mode": "inline_once", "username": "demo", "password": "secret"},
                    "operation": "vehicle_reachability",
                    "params": {},
                    "timeout_sec": 5,
                }
            )

            for _ in range(200):
                result = service.get_job(created["job_id"])
                if result["state"] in {"finished", "failed", "timeout"}:
                    break
                time.sleep(0.01)
            else:
                raise AssertionError("runner job did not finish")

            self.assertEqual(status, 201)
            self.assertEqual(created["request_id"], "runner-job-1")
            self.assertEqual(result["state"], "finished")
            self.assertEqual(result["operation"], "vehicle_reachability")
            self.assertNotIn("secret", json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
