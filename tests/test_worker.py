from __future__ import annotations

import json
import os
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from bo_vpn_agent.api import validate_service_headers
from bo_vpn_agent.command_exec import CommandResult, SubprocessCommandExecutor
from bo_vpn_agent.config import RunnerDaemonConfig, WorkerConfig
from bo_vpn_agent.runner_daemon import RunnerDaemonService
from bo_vpn_agent.service import WorkerService
from bo_vpn_agent.vehicle_inventory import VehicleInventory


def make_config(tmp_path: Path, runner_mode: str = "dry_run") -> WorkerConfig:
    return WorkerConfig(
        service_token="test-token",
        runner_mode=runner_mode,
        audit_log_path=tmp_path / "audit.log",
        artifact_dir=tmp_path / "artifacts",
        global_create_limit_per_minute=100,
        user_create_limit_per_minute=100,
    )


def make_config_with_inventory(tmp_path: Path, inventory_path: Path, runner_mode: str = "dry_run") -> WorkerConfig:
    config = make_config(tmp_path, runner_mode)
    config.vehicle_inventory_path = inventory_path
    return config


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


def wait_for_job_terminal(service: RunnerDaemonService, job_id: str) -> dict[str, object]:
    for _ in range(200):
        response = service.get_job(job_id)
        if response["state"] in {"finished", "failed", "timeout"}:
            return response
        time.sleep(0.01)
    raise AssertionError("runner job did not finish")


class FakeCommandExecutor:
    def __init__(self, results: list[CommandResult]) -> None:
        self.results = list(results)
        self.calls: list[tuple[str, ...]] = []

    def run(self, args: list[str] | tuple[str, ...], timeout_sec: int) -> CommandResult:
        self.calls.append(tuple(args))
        if not self.results:
            raise AssertionError("unexpected command execution")
        return self.results.pop(0)


def command_result(stdout: str = "", stderr: str = "", returncode: int = 0, timed_out: bool = False) -> CommandResult:
    return CommandResult(args=(), returncode=returncode, stdout=stdout, stderr=stderr, timed_out=timed_out)


def write_inventory(tmp_path: Path, rows: list[dict[str, str]]) -> Path:
    path = tmp_path / "vehicles.csv"
    headers = ["garage_number", "vehicle_id", "plate", "ip", "mac", "model", "branch", "updated_at", "comment"]
    lines = [",".join(headers)]
    for row in rows:
        lines.append(",".join(row.get(header, "") for header in headers))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def inventory_row(**overrides: str) -> dict[str, str]:
    row = {
        "garage_number": "6217",
        "vehicle_id": "81006217",
        "plate": "Р 022 КС 198",
        "ip": "172.26.129.119",
        "mac": "c4:00:ad:77:50:d3",
        "model": "ВЛБ 28с",
        "branch": "Екатерининский Вест-Сервис",
        "updated_at": "2026-06-30T07:00:00+03:00",
        "comment": "PA-01",
    }
    row.update(overrides)
    return row


class WorkerServiceTests(unittest.TestCase):
    def test_subprocess_command_executor_limits_output(self) -> None:
        executor = SubprocessCommandExecutor(max_output_bytes=10)

        result = executor.run([sys.executable, "-c", "print('x' * 100)"], timeout_sec=5)

        self.assertEqual(result.returncode, 0)
        self.assertLessEqual(len(result.stdout.encode("utf-8")), 10)
        self.assertTrue(result.output_truncated)

    def test_worker_config_reads_current_env_names(self) -> None:
        keys = {
            "BO_VPN_WORKER_AUTH_TOKEN": "token-from-env",
            "BO_VPN_DEFAULT_RUNNER_MODE": "existing_container",
            "BO_VPN_RUNNER_URL": "http://127.0.0.1:8091",
            "BO_VPN_AUDIT_LOG_PATH": "logs/audit.jsonl",
            "BO_VPN_VEHICLE_INVENTORY_PATH": "config/vehicles.csv",
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
        self.assertEqual(config.vehicle_inventory_path, Path("config/vehicles.csv"))

    def test_vehicle_inventory_loads_valid_csv(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            path = write_inventory(Path(raw_tmp), [inventory_row()])

            record = VehicleInventory(path).resolve_query("6217")

            self.assertEqual(record.garage_number, "6217")
            self.assertEqual(record.vehicle_id, "81006217")
            self.assertEqual(record.ip, "172.26.129.119")

    def test_vehicle_inventory_resolves_by_garage_number(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            path = write_inventory(Path(raw_tmp), [inventory_row()])

            record = VehicleInventory(path).resolve_query("6217")

            self.assertEqual(record.ip, "172.26.129.119")

    def test_vehicle_inventory_resolves_by_vehicle_id(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            path = write_inventory(Path(raw_tmp), [inventory_row()])

            record = VehicleInventory(path).resolve_query("81006217")

            self.assertEqual(record.garage_number, "6217")

    def test_vehicle_inventory_resolves_by_plate_with_normalized_spaces(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            path = write_inventory(Path(raw_tmp), [inventory_row()])

            record = VehicleInventory(path).resolve_query("Р   022   КС   198")

            self.assertEqual(record.ip, "172.26.129.119")

    def test_vehicle_number_resolves_as_garage_number_first(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            path = write_inventory(
                Path(raw_tmp),
                [
                    inventory_row(garage_number="6217", vehicle_id="garage-match", ip="172.26.129.119"),
                    inventory_row(garage_number="9999", vehicle_id="6217", ip="172.26.129.120"),
                ],
            )

            record = VehicleInventory(path).resolve_vehicle({"number": "6217"})

            self.assertEqual(record.ip, "172.26.129.119")

    def test_vehicle_inventory_invalid_ip_returns_vehicle_ip_not_found(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            path = write_inventory(Path(raw_tmp), [inventory_row(ip="not-an-ip")])

            with self.assertRaises(Exception) as raised:
                VehicleInventory(path).resolve_query("6217")

            self.assertEqual(getattr(raised.exception, "error_code"), "vehicle_ip_not_found")

    def test_vehicle_inventory_not_found_returns_vehicle_ip_not_found(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            path = write_inventory(Path(raw_tmp), [inventory_row()])

            with self.assertRaises(Exception) as raised:
                VehicleInventory(path).resolve_query("missing")

            self.assertEqual(getattr(raised.exception, "error_code"), "vehicle_ip_not_found")

    def test_vehicle_inventory_ambiguous_records_return_normalized_error(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            path = write_inventory(
                Path(raw_tmp),
                [
                    inventory_row(ip="172.26.129.119"),
                    inventory_row(vehicle_id="81006218", ip="172.26.129.120"),
                ],
            )

            with self.assertRaises(Exception) as raised:
                VehicleInventory(path).resolve_query("6217")

            self.assertEqual(getattr(raised.exception, "error_code"), "vehicle_inventory_ambiguous")

    def test_runner_config_reads_existing_container_env(self) -> None:
        keys = {
            "BO_VPN_EXISTING_CONTAINER_NAME": "univpn-service-test",
            "BO_VPN_NSENTER_BIN": "/custom/nsenter",
            "BO_VPN_DOCKER_BIN": "/custom/docker",
            "BO_VPN_SSH_BIN": "/custom/ssh",
            "BO_VPN_SSH_KEY_PATH": "/keys/rsa.key",
            "BO_VPN_DEFAULT_SSH_USER": "root-test",
            "BO_VPN_NSENTER_TIMEOUT_SEC": "9",
            "BO_VPN_COMMAND_OUTPUT_MAX_BYTES": "12345",
        }
        previous = {key: os.environ.get(key) for key in keys}
        try:
            os.environ.update(keys)
            config = RunnerDaemonConfig.from_env()
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.assertEqual(config.existing_container_name, "univpn-service-test")
        self.assertEqual(config.nsenter_bin, "/custom/nsenter")
        self.assertEqual(config.docker_bin, "/custom/docker")
        self.assertEqual(config.ssh_bin, "/custom/ssh")
        self.assertEqual(config.ssh_key_path, Path("/keys/rsa.key"))
        self.assertEqual(config.default_ssh_user, "root-test")
        self.assertEqual(config.nsenter_timeout_sec, 9)
        self.assertEqual(config.command_output_max_bytes, 12345)

    def test_runner_config_reads_managed_vpn_env(self) -> None:
        keys = {
            "BO_VPN_MANAGE_VPN_SESSION": "true",
            "BO_VPN_STOP_VPN_AFTER_TASK": "1",
            "BO_VPN_UNIVPN_CONTROL_PATH": "/run/univpn/custom.in",
            "BO_VPN_UNIVPN_LOGIN_TIMEOUT_SEC": "12",
            "BO_VPN_UNIVPN_CONNECT_POLL_INTERVAL_SEC": "0.5",
            "BO_VPN_UNIVPN_ROUTE_CIDR": "172.26.0.0/15",
            "BO_VPN_UNIVPN_INTERFACE": "cnem_vnic",
            "BO_VPN_UNIVPN_LOGIN_MODE": "container_secret",
            "BO_VPN_UNIVPN_SECRET_PATH": "/run/secrets/custom.env",
            "BO_VPN_UNIVPN_DISCONNECT_SEQUENCE": "q",
        }
        previous = {key: os.environ.get(key) for key in keys}
        try:
            os.environ.update(keys)
            config = RunnerDaemonConfig.from_env()
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.assertTrue(config.manage_vpn_session)
        self.assertTrue(config.stop_vpn_after_task)
        self.assertEqual(config.univpn_control_path, Path("/run/univpn/custom.in"))
        self.assertEqual(config.univpn_login_timeout_sec, 12)
        self.assertEqual(config.univpn_connect_poll_interval_sec, 0.5)
        self.assertEqual(config.univpn_route_cidr, "172.26.0.0/15")
        self.assertEqual(config.univpn_interface, "cnem_vnic")
        self.assertEqual(config.univpn_login_mode, "container_secret")
        self.assertEqual(config.univpn_secret_path, Path("/run/secrets/custom.env"))
        self.assertEqual(config.univpn_disconnect_sequence, "q")

    def test_runner_config_defaults_use_shared_univpn_control_and_secret_file(self) -> None:
        config = RunnerDaemonConfig()

        self.assertEqual(config.univpn_control_path, Path("/run/univpn/univpn.in"))
        self.assertEqual(config.univpn_secret_path, Path("/run/secrets/univpn.env"))

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

    def test_create_task_resolves_vehicle_ip_from_inventory(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            tmp_path = Path(raw_tmp)
            inventory_path = write_inventory(tmp_path, [inventory_row()])
            service = WorkerService(make_config_with_inventory(tmp_path, inventory_path))
            payload = task_payload(operation="vehicle_reachability")
            payload["vehicle"] = {"number": "6217"}

            status, created = service.create_task(payload)
            result = wait_for_terminal(service, created["task_id"])

            self.assertEqual(status, 201)
            self.assertEqual(result["state"], "finished")
            self.assertEqual(result["vehicle"], {"number": "6217", "ip": "172.26.129.119"})

    def test_create_task_inventory_not_found_returns_vehicle_ip_not_found(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            tmp_path = Path(raw_tmp)
            inventory_path = write_inventory(tmp_path, [inventory_row()])
            service = WorkerService(make_config_with_inventory(tmp_path, inventory_path))
            payload = task_payload(operation="vehicle_reachability")
            payload["vehicle"] = {"number": "missing-vehicle"}

            with self.assertRaises(Exception) as raised:
                service.create_task(payload)

            self.assertEqual(getattr(raised.exception, "status"), 422)
            self.assertEqual(getattr(raised.exception, "error_code"), "vehicle_ip_not_found")
            self.assertNotIn("secret-password", json.dumps(raised.exception.to_response(), ensure_ascii=False))

    def test_create_task_inventory_ambiguous_returns_normalized_error(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            tmp_path = Path(raw_tmp)
            inventory_path = write_inventory(
                tmp_path,
                [
                    inventory_row(ip="172.26.129.119"),
                    inventory_row(vehicle_id="81006218", ip="172.26.129.120"),
                ],
            )
            service = WorkerService(make_config_with_inventory(tmp_path, inventory_path))
            payload = task_payload(operation="vehicle_reachability")
            payload["vehicle"] = {"number": "6217"}

            with self.assertRaises(Exception) as raised:
                service.create_task(payload)

            self.assertEqual(getattr(raised.exception, "status"), 409)
            self.assertEqual(getattr(raised.exception, "error_code"), "vehicle_inventory_ambiguous")
            self.assertNotIn("secret-password", json.dumps(raised.exception.to_response(), ensure_ascii=False))

    def test_create_task_with_explicit_vehicle_ip_keeps_current_behavior(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            service = WorkerService(make_config(Path(raw_tmp)))
            payload = task_payload(operation="vehicle_reachability")
            payload["vehicle"] = {"number": "6217", "ip": "203.0.113.10"}

            status, created = service.create_task(payload)
            result = wait_for_terminal(service, created["task_id"])

            self.assertEqual(status, 201)
            self.assertEqual(result["vehicle"], {"number": "6217", "ip": "203.0.113.10"})

    def test_vehicle_resolve_returns_record_without_vpn_secrets(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            tmp_path = Path(raw_tmp)
            inventory_path = write_inventory(tmp_path, [inventory_row()])
            service = WorkerService(make_config_with_inventory(tmp_path, inventory_path))

            response = service.resolve_vehicle("6217")
            body = json.dumps(response, ensure_ascii=False)

            self.assertEqual(response["vehicle"]["ip"], "172.26.129.119")
            self.assertNotIn("secret-password", body)
            self.assertNotIn("smoke-password", body)

    def test_vehicle_resolve_requires_service_auth_and_request_id_contract(self) -> None:
        validate_service_headers({"Authorization": "Bearer test-token", "X-Request-Id": "resolve-test"}, "test-token")

        with self.assertRaises(Exception) as no_auth:
            validate_service_headers({"X-Request-Id": "resolve-test"}, "test-token")
        self.assertEqual(getattr(no_auth.exception, "status"), 401)

        with self.assertRaises(Exception) as no_request_id:
            validate_service_headers({"Authorization": "Bearer test-token"}, "test-token")
        self.assertEqual(getattr(no_request_id.exception, "status"), 400)

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

    def test_existing_container_vehicle_reachability_returns_tcp_statuses(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            executor = FakeCommandExecutor(
                [
                    command_result(stdout="1234\n"),
                    *_preflight_success(),
                    command_result(stdout='{"22":"open","443":"open","80":"closed"}\n'),
                ]
            )
            service = RunnerDaemonService(_runner_config(Path(raw_tmp)), command_executor=executor)
            status, created = service.create_job(_runner_job_payload(operation="vehicle_reachability"))
            result = wait_for_job_terminal(service, created["job_id"])
            body = json.dumps(result, ensure_ascii=False)

            self.assertEqual(status, 201)
            self.assertEqual(result["state"], "finished")
            self.assertEqual(result["data"], {"tcp_22": "open", "tcp_443": "open", "tcp_80": "closed"})
            self.assertNotIn("secret-password", body)
            self.assertEqual(executor.calls[0][:4], ("/usr/bin/docker", "inspect", "-f", "{{.State.Pid}}"))
            self.assertEqual(executor.calls[1][:4], ("/usr/bin/nsenter", "-t", "1234", "-n"))
            self.assertEqual(executor.calls[1][4:], ("ip", "addr", "show", "cnem_vnic"))
            self.assertEqual(executor.calls[2][4:], ("ip", "route"))

    def test_existing_container_docker_inspect_failure_returns_vpn_client_error(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            executor = FakeCommandExecutor([command_result(stderr="no such container", returncode=1)])
            service = RunnerDaemonService(_runner_config(Path(raw_tmp)), command_executor=executor)
            _, created = service.create_job(_runner_job_payload(operation="vehicle_reachability"))
            result = wait_for_job_terminal(service, created["job_id"])

            self.assertEqual(result["state"], "failed")
            self.assertEqual(result["error_code"], "vpn_client_error")

    def test_existing_container_nsenter_timeout_returns_operation_timeout(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            executor = FakeCommandExecutor(
                [
                    command_result(stdout="1234\n"),
                    command_result(timed_out=True, returncode=124),
                ]
            )
            service = RunnerDaemonService(_runner_config(Path(raw_tmp)), command_executor=executor)
            _, created = service.create_job(_runner_job_payload(operation="vehicle_reachability"))
            result = wait_for_job_terminal(service, created["job_id"])

            self.assertEqual(result["state"], "failed")
            self.assertEqual(result["error_code"], "operation_timeout")

    def test_existing_container_missing_cnem_vnic_returns_vpn_client_error(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            executor = FakeCommandExecutor(
                [
                    command_result(stdout="1234\n"),
                    command_result(stderr='Device "cnem_vnic" does not exist.', returncode=1),
                ]
            )
            service = RunnerDaemonService(_runner_config(Path(raw_tmp)), command_executor=executor)
            _, created = service.create_job(_runner_job_payload(operation="vehicle_reachability"))
            result = wait_for_job_terminal(service, created["job_id"])

            self.assertEqual(result["state"], "failed")
            self.assertEqual(result["error_code"], "vpn_client_error")

    def test_existing_container_preflight_nsenter_permission_denied_is_explicit(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            executor = FakeCommandExecutor(
                [
                    command_result(stdout="1234\n"),
                    command_result(stderr="nsenter: reassociate to namespace 'ns/net' failed: Permission denied", returncode=1),
                ]
            )
            service = RunnerDaemonService(_runner_config(Path(raw_tmp)), command_executor=executor)
            _, created = service.create_job(_runner_job_payload(operation="vehicle_reachability"))
            result = wait_for_job_terminal(service, created["job_id"])
            message = str(result["message"]).lower()

            self.assertEqual(result["state"], "failed")
            self.assertEqual(result["error_code"], "vpn_client_error")
            self.assertIn("permission denied", message)
            self.assertIn("nsenter", message)

    def test_existing_container_missing_vpn_route_returns_vpn_client_error(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            executor = FakeCommandExecutor(
                [
                    command_result(stdout="1234\n"),
                    command_result(stdout="cnem_vnic        UNKNOWN        192.168.122.203/28\n"),
                    command_result(stdout="default via 172.17.0.1 dev eth0\n172.17.0.0/16 dev eth0\n"),
                ]
            )
            service = RunnerDaemonService(_runner_config(Path(raw_tmp)), command_executor=executor)
            _, created = service.create_job(_runner_job_payload(operation="vehicle_reachability"))
            result = wait_for_job_terminal(service, created["job_id"])

            self.assertEqual(result["state"], "failed")
            self.assertEqual(result["error_code"], "vpn_client_error")

    def test_existing_container_all_tcp_ports_unavailable_returns_vehicle_unreachable(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            executor = FakeCommandExecutor(
                [
                    command_result(stdout="1234\n"),
                    *_preflight_success(),
                    command_result(stdout='{"22":"closed","443":"closed","80":"closed"}\n'),
                ]
            )
            service = RunnerDaemonService(_runner_config(Path(raw_tmp)), command_executor=executor)
            _, created = service.create_job(_runner_job_payload(operation="vehicle_reachability"))
            result = wait_for_job_terminal(service, created["job_id"])

            self.assertEqual(result["state"], "failed")
            self.assertEqual(result["error_code"], "vehicle_unreachable")

    def test_existing_container_basic_status_returns_raw_fields(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            executor = FakeCommandExecutor(
                [
                    command_result(stdout="1234\n"),
                    *_preflight_success(),
                    command_result(
                        stdout=(
                            "mic\n"
                            " 10:15:42 up 2 days,  4:11,  1 user,  load average: 0.10, 0.08, 0.05\n"
                            "2026-06-29T10:15:42+00:00\n"
                            "Filesystem      Size  Used Avail Use% Mounted on\n"
                            "/dev/root        20G  8.0G   12G  41% /\n"
                            "              total        used        free\n"
                            "Mem:           1024         512         256\n"
                        )
                    ),
                ]
            )
            service = RunnerDaemonService(_runner_config(Path(raw_tmp)), command_executor=executor)
            _, created = service.create_job(_runner_job_payload(operation="basic_status"))
            result = wait_for_job_terminal(service, created["job_id"])

            self.assertEqual(result["state"], "finished")
            self.assertEqual(result["data"]["hostname"], "mic")
            self.assertIn("up 2 days", result["data"]["uptime_raw"])
            self.assertEqual(result["data"]["system_time"], "2026-06-29T10:15:42+00:00")
            self.assertIn("/dev/root", result["data"]["disk_root_raw"])
            self.assertIn("Mem:", result["data"]["memory_raw"])

    def test_existing_container_basic_status_ssh_failure_returns_ssh_failed(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            executor = FakeCommandExecutor(
                [
                    command_result(stdout="1234\n"),
                    *_preflight_success(),
                    command_result(stderr="permission denied", returncode=255),
                ]
            )
            service = RunnerDaemonService(_runner_config(Path(raw_tmp)), command_executor=executor)
            _, created = service.create_job(_runner_job_payload(operation="basic_status"))
            result = wait_for_job_terminal(service, created["job_id"])

            self.assertEqual(result["state"], "failed")
            self.assertEqual(result["error_code"], "ssh_failed")

    def test_container_namespace_runner_does_not_call_docker_or_nsenter(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            executor = FakeCommandExecutor(
                [
                    *_container_preflight_success(),
                    command_result(stdout='{"22":"open","443":"closed","80":"closed"}\n'),
                ]
            )
            service = RunnerDaemonService(_container_runner_config(Path(raw_tmp)), command_executor=executor)
            _, created = service.create_job(_runner_job_payload(operation="vehicle_reachability", runner_mode="container_namespace"))
            result = wait_for_job_terminal(service, created["job_id"])

            self.assertEqual(result["state"], "finished")
            self.assertEqual(result["data"]["tcp_22"], "open")
            flattened_calls = " ".join(" ".join(call) for call in executor.calls)
            self.assertNotIn("docker", flattened_calls)
            self.assertNotIn("nsenter", flattened_calls)

    def test_container_namespace_preflight_checks_interface_and_route(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            executor = FakeCommandExecutor(
                [
                    *_container_preflight_success(),
                    command_result(stdout='{"22":"open","443":"closed","80":"closed"}\n'),
                ]
            )
            service = RunnerDaemonService(_container_runner_config(Path(raw_tmp)), command_executor=executor)
            _, created = service.create_job(_runner_job_payload(operation="vehicle_reachability", runner_mode="container_namespace"))
            wait_for_job_terminal(service, created["job_id"])

            self.assertEqual(executor.calls[0], ("ip", "-br", "addr", "show", "cnem_vnic"))
            self.assertEqual(executor.calls[1], ("ip", "route"))

    def test_container_namespace_basic_status_uses_plain_ssh_path(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            executor = FakeCommandExecutor(
                [
                    *_container_preflight_success(),
                    command_result(
                        stdout=(
                            "mic\n"
                            " up 1 day\n"
                            "2026-06-30T10:00:00+03:00\n"
                            "Filesystem Size Used Avail Use% Mounted on\n"
                            "/dev/root 20G 8G 12G 41% /\n"
                            "total used free\n"
                            "Mem: 1024 512 256\n"
                        )
                    ),
                ]
            )
            service = RunnerDaemonService(_container_runner_config(Path(raw_tmp)), command_executor=executor)
            _, created = service.create_job(_runner_job_payload(operation="basic_status", runner_mode="container_namespace"))
            result = wait_for_job_terminal(service, created["job_id"])

            self.assertEqual(result["state"], "finished")
            self.assertEqual(executor.calls[2][0], "/usr/bin/ssh")
            self.assertNotIn("/usr/bin/nsenter", executor.calls[2])

    def test_container_namespace_managed_login_writes_sequence_without_api_secret_leak(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            tmp_path = Path(raw_tmp)
            secret_path = tmp_path / "univpn.env"
            control_path = tmp_path / "univpn.in"
            secret_path.write_text("VPN_USERNAME=stand-user\nVPN_PASSWORD=stand-password\n", encoding="utf-8")
            executor = FakeCommandExecutor(
                [
                    command_result(stderr='Device "cnem_vnic" does not exist.', returncode=1),
                    *_container_preflight_success(),
                    command_result(stdout='{"22":"open","443":"closed","80":"closed"}\n'),
                ]
            )
            service = RunnerDaemonService(
                _container_runner_config(
                    tmp_path,
                    manage_vpn_session=True,
                    univpn_secret_path=secret_path,
                    univpn_control_path=control_path,
                    univpn_login_timeout_sec=0,
                    univpn_connect_poll_interval_sec=0,
                ),
                command_executor=executor,
            )
            _, created = service.create_job(
                _runner_job_payload(operation="vehicle_reachability", runner_mode="container_namespace", vpn_mode="container_secret")
            )
            result = wait_for_job_terminal(service, created["job_id"])
            body = json.dumps(result, ensure_ascii=False)

            self.assertEqual(result["state"], "finished")
            self.assertEqual(control_path.read_text(encoding="utf-8"), "3\n1\nstand-user\nstand-password\n")
            self.assertNotIn("stand-password", body)
            self.assertNotIn("stand-user", body)

    def test_container_namespace_skips_login_when_already_connected(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            tmp_path = Path(raw_tmp)
            control_path = tmp_path / "univpn.in"
            executor = FakeCommandExecutor(
                [
                    *_container_preflight_success(),
                    command_result(stdout='{"22":"open","443":"closed","80":"closed"}\n'),
                ]
            )
            service = RunnerDaemonService(
                _container_runner_config(tmp_path, manage_vpn_session=True, univpn_control_path=control_path),
                command_executor=executor,
            )
            _, created = service.create_job(
                _runner_job_payload(operation="vehicle_reachability", runner_mode="container_namespace", vpn_mode="container_secret")
            )
            result = wait_for_job_terminal(service, created["job_id"])

            self.assertEqual(result["state"], "finished")
            self.assertFalse(control_path.exists())

    def test_container_namespace_missing_route_after_login_returns_vpn_client_error(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            tmp_path = Path(raw_tmp)
            secret_path = tmp_path / "univpn.env"
            secret_path.write_text("VPN_USERNAME=stand-user\nVPN_PASSWORD=stand-password\n", encoding="utf-8")
            executor = FakeCommandExecutor(
                [
                    command_result(stderr='Device "cnem_vnic" does not exist.', returncode=1),
                    command_result(stdout="cnem_vnic UNKNOWN 192.168.122.203/28\n"),
                    command_result(stdout="default via 172.18.0.1 dev eth0\n"),
                ]
            )
            service = RunnerDaemonService(
                _container_runner_config(
                    tmp_path,
                    manage_vpn_session=True,
                    univpn_secret_path=secret_path,
                    univpn_login_timeout_sec=0,
                    univpn_connect_poll_interval_sec=0,
                ),
                command_executor=executor,
            )
            _, created = service.create_job(
                _runner_job_payload(operation="vehicle_reachability", runner_mode="container_namespace", vpn_mode="container_secret")
            )
            result = wait_for_job_terminal(service, created["job_id"])

            self.assertEqual(result["state"], "failed")
            self.assertEqual(result["error_code"], "vpn_client_error")

    def test_container_namespace_cleanup_is_called_on_success(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            tmp_path = Path(raw_tmp)
            control_path = tmp_path / "univpn.in"
            executor = FakeCommandExecutor(
                [
                    *_container_preflight_success(),
                    command_result(stdout='{"22":"open","443":"closed","80":"closed"}\n'),
                ]
            )
            service = RunnerDaemonService(
                _container_runner_config(
                    tmp_path,
                    stop_vpn_after_task=True,
                    univpn_control_path=control_path,
                    univpn_disconnect_sequence="q",
                ),
                command_executor=executor,
            )
            _, created = service.create_job(_runner_job_payload(operation="vehicle_reachability", runner_mode="container_namespace"))
            result = wait_for_job_terminal(service, created["job_id"])

            self.assertEqual(result["state"], "finished")
            self.assertEqual(control_path.read_text(encoding="utf-8"), "q\n")

    def test_container_namespace_cleanup_is_called_on_failure(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            tmp_path = Path(raw_tmp)
            control_path = tmp_path / "univpn.in"
            executor = FakeCommandExecutor(
                [
                    *_container_preflight_success(),
                    command_result(stdout='{"22":"closed","443":"closed","80":"closed"}\n'),
                ]
            )
            service = RunnerDaemonService(
                _container_runner_config(
                    tmp_path,
                    stop_vpn_after_task=True,
                    univpn_control_path=control_path,
                    univpn_disconnect_sequence="q",
                ),
                command_executor=executor,
            )
            _, created = service.create_job(_runner_job_payload(operation="vehicle_reachability", runner_mode="container_namespace"))
            result = wait_for_job_terminal(service, created["job_id"])

            self.assertEqual(result["state"], "failed")
            self.assertEqual(result["error_code"], "vehicle_unreachable")
            self.assertEqual(control_path.read_text(encoding="utf-8"), "q\n")

    def test_container_namespace_cleanup_failure_adds_warning_to_success(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            tmp_path = Path(raw_tmp)
            bad_parent = tmp_path / "not-a-directory"
            bad_parent.write_text("file", encoding="utf-8")
            executor = FakeCommandExecutor(
                [
                    *_container_preflight_success(),
                    command_result(stdout='{"22":"open","443":"closed","80":"closed"}\n'),
                ]
            )
            service = RunnerDaemonService(
                _container_runner_config(
                    tmp_path,
                    stop_vpn_after_task=True,
                    univpn_control_path=bad_parent / "univpn.in",
                    univpn_disconnect_sequence="q",
                ),
                command_executor=executor,
            )
            _, created = service.create_job(_runner_job_payload(operation="vehicle_reachability", runner_mode="container_namespace"))
            result = wait_for_job_terminal(service, created["job_id"])

            self.assertEqual(result["state"], "finished")
            self.assertIn("UniVPN cleanup failed", result["warnings"])

    def test_bot_api_contract_doc_matches_current_schema(self) -> None:
        doc = Path("docs/bot_worker_api.md").read_text(encoding="utf-8")

        self.assertIn("POST /tasks", doc)
        self.assertIn('"telegram_user_id"', doc)
        self.assertIn('"operation": "vehicle_reachability"', doc)
        self.assertIn('"operation": "basic_status"', doc)
        self.assertIn('"runner_mode": "container_namespace"', doc)
        self.assertIn('"mode": "container_secret"', doc)

    def test_compose_design_doc_marks_full_compose_experimental(self) -> None:
        doc = Path("docs/compose_vpn_runner_design.md").read_text(encoding="utf-8").lower()

        self.assertIn("docker-compose.full.yml", doc)
        self.assertIn("experimental", doc)
        self.assertIn("must not be treated as production-ready", doc)

    def test_full_compose_discards_univpn_console_session(self) -> None:
        compose = Path("docker-compose.full.yml").read_text(encoding="utf-8")

        self.assertNotIn("/var/log/univpn/univpn-console.log", compose)
        self.assertIn('script -qfec "su - vpn -c /usr/local/UniVPN/serviceclient/UniVPNCS" /dev/null', compose)
        self.assertIn(">/dev/null 2>&1", compose)

    def test_docs_warn_about_old_univpn_logs_and_credential_rotation(self) -> None:
        readme = Path("README.md").read_text(encoding="utf-8").lower()
        design = Path("docs/compose_vpn_runner_design.md").read_text(encoding="utf-8").lower()

        self.assertIn("old unsafe", readme)
        self.assertIn("rotate", readme)
        self.assertIn("credential", readme)
        self.assertIn("secret logging hardening", design)
        self.assertIn("console session", design)
        self.assertIn("/dev/null", design)

def _runner_config(tmp_path: Path) -> RunnerDaemonConfig:
    return RunnerDaemonConfig(
        artifact_dir=tmp_path / "runner-artifacts",
        artifact_ttl_hours=24,
        existing_container_name="univpn-service",
        nsenter_bin="/usr/bin/nsenter",
        docker_bin="/usr/bin/docker",
        ssh_bin="/usr/bin/ssh",
        ssh_key_path=Path("/home/timur/univpn/rsa.key"),
        default_ssh_user="root",
        nsenter_timeout_sec=8,
        command_output_max_bytes=64 * 1024,
    )


def _container_runner_config(
    tmp_path: Path,
    manage_vpn_session: bool = False,
    stop_vpn_after_task: bool = False,
    univpn_control_path: Path | None = None,
    univpn_secret_path: Path | None = None,
    univpn_login_timeout_sec: int = 45,
    univpn_connect_poll_interval_sec: float = 0,
    univpn_disconnect_sequence: str | None = None,
) -> RunnerDaemonConfig:
    return RunnerDaemonConfig(
        artifact_dir=tmp_path / "runner-artifacts",
        artifact_ttl_hours=24,
        ssh_bin="/usr/bin/ssh",
        ssh_key_path=Path("/run/keys/rsa.key"),
        default_ssh_user="root",
        nsenter_timeout_sec=8,
        command_output_max_bytes=64 * 1024,
        manage_vpn_session=manage_vpn_session,
        stop_vpn_after_task=stop_vpn_after_task,
        univpn_control_path=univpn_control_path or tmp_path / "univpn.in",
        univpn_secret_path=univpn_secret_path or tmp_path / "univpn.env",
        univpn_login_timeout_sec=univpn_login_timeout_sec,
        univpn_connect_poll_interval_sec=univpn_connect_poll_interval_sec,
        univpn_disconnect_sequence=univpn_disconnect_sequence,
    )


def _runner_job_payload(operation: str, runner_mode: str = "existing_container", vpn_mode: str = "inline_once") -> dict[str, object]:
    if vpn_mode == "container_secret":
        vpn = {"mode": "container_secret"}
    else:
        vpn = {"mode": "inline_once", "username": "demo", "password": "secret-password"}
    return {
        "request_id": f"{runner_mode}-{operation}",
        "runner_mode": runner_mode,
        "vehicle": {"number": "6968", "ip": "172.26.130.165"},
        "vpn": vpn,
        "operation": operation,
        "params": {},
        "timeout_sec": 8,
    }


def _preflight_success() -> list[CommandResult]:
    return [
        command_result(stdout="cnem_vnic        UNKNOWN        192.168.122.203/28\n"),
        command_result(
            stdout=(
                "10.208.0.0/16 via 192.168.122.203 dev cnem_vnic\n"
                "10.224.0.0/11 via 192.168.122.203 dev cnem_vnic\n"
                "172.26.0.0/15 via 192.168.122.203 dev cnem_vnic\n"
                "192.168.100.0/22 via 192.168.122.203 dev cnem_vnic\n"
            )
        ),
    ]


def _container_preflight_success() -> list[CommandResult]:
    return [
        command_result(stdout="cnem_vnic UNKNOWN 192.168.122.203/28\n"),
        command_result(
            stdout=(
                "172.26.0.0/15 via 192.168.122.203 dev cnem_vnic\n"
                "192.168.100.0/22 via 192.168.122.203 dev cnem_vnic\n"
            )
        ),
    ]


if __name__ == "__main__":
    unittest.main()
