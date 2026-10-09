from __future__ import annotations

import argparse
import importlib.util
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


MODULE_PATH = Path(__file__).parent.parent / "scripts" / "vpn_doctor.py"
SPEC = importlib.util.spec_from_file_location("vpn_doctor", MODULE_PATH)
vpn_doctor = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = vpn_doctor
SPEC.loader.exec_module(vpn_doctor)


def args(repo: str) -> argparse.Namespace:
    return argparse.Namespace(repo=repo, compose_file="docker-compose.full.yml", canary_ip=None,
                              mode="status", json=False, quiet=False)


class VpnDoctorTests(unittest.TestCase):
    def test_redacts_entire_sensitive_lines(self) -> None:
        cleaned = vpn_doctor.VpnDoctor.clean(
            "phase connected\nAuthorization: Bearer visible-value\n"
            "https://api.telegram.org/bot123456:abcdef/getMe\npassword=hunter2"
        )
        self.assertIn("phase connected", cleaned)
        self.assertNotIn("visible-value", cleaned)
        self.assertNotIn("abcdef", cleaned)
        self.assertNotIn("hunter2", cleaned)
        self.assertIn("/bot[REDACTED]/getMe", cleaned)
        self.assertEqual(cleaned.count("[redacted sensitive log line]"), 2)

    def test_log_rotation_is_bounded(self) -> None:
        with TemporaryDirectory() as directory, patch.dict(os.environ, {
            "BO_VPN_DOCTOR_LOG": str(Path(directory) / "doctor.log"),
            "BO_VPN_DOCTOR_STATE": str(Path(directory) / "doctor.state"),
            "BO_VPN_DOCTOR_LOG_MAX_BYTES": "100",
            "BO_VPN_DOCTOR_LOG_BACKUPS": "2",
        }):
            doctor = vpn_doctor.VpnDoctor(args(directory))
            for index in range(20):
                doctor.log("ERROR", "synthetic", f"failure-{index}-" + "x" * 80)
            files = sorted(Path(directory).glob("doctor.log*"))
            self.assertEqual([item.name for item in files], ["doctor.log", "doctor.log.1", "doctor.log.2"])
            self.assertFalse((Path(directory) / "doctor.log.3").exists())

    def test_repeated_healthy_state_does_not_append(self) -> None:
        with TemporaryDirectory() as directory, patch.dict(os.environ, {
            "BO_VPN_DOCTOR_LOG": str(Path(directory) / "doctor.log"),
            "BO_VPN_DOCTOR_STATE": str(Path(directory) / "doctor.state"),
        }):
            doctor = vpn_doctor.VpnDoctor(args(directory))
            report = vpn_doctor.Report([vpn_doctor.Check("vpn", True, "ready")])
            doctor.record_state(report)
            size = doctor.log_path.stat().st_size
            doctor.record_state(report)
            self.assertEqual(doctor.log_path.stat().st_size, size)

    def test_canary_validation_happens_before_worker_request(self) -> None:
        with TemporaryDirectory() as directory, patch.dict(os.environ, {
            "BO_VPN_DOCTOR_LOG": str(Path(directory) / "doctor.log"),
            "BO_VPN_DOCTOR_STATE": str(Path(directory) / "doctor.state"),
        }):
            doctor = vpn_doctor.VpnDoctor(args(directory))
            with patch.object(doctor, "worker_request") as request:
                with self.assertRaisesRegex(vpn_doctor.DoctorError, "172.26.0.0/15"):
                    doctor.trigger_login("8.8.8.8")
                request.assert_not_called()

    def test_repository_never_collects_univpn_logs(self) -> None:
        source = MODULE_PATH.read_text(encoding="utf-8")
        capture = source[source.index("def capture_failure_context"):source.index("def repair")]
        self.assertNotIn('"univpn-service"', capture)
        self.assertIn("never collected", capture)

    def test_ssh_helper_only_runs_preflight(self) -> None:
        helper = (MODULE_PATH.parent / "vpn_ssh.sh").read_text(encoding="utf-8")
        self.assertIn('vpn_doctor.py" preflight --quiet', helper)
        self.assertNotIn('vpn_doctor.py" repair', helper)
        self.assertIn("BO_VPN_PREFLIGHT", helper)

    def test_preflight_does_not_call_worker_or_compose(self) -> None:
        with TemporaryDirectory() as directory, patch.dict(os.environ, {
            "BO_VPN_DOCTOR_LOG": str(Path(directory) / "doctor.log"),
            "BO_VPN_DOCTOR_STATE": str(Path(directory) / "doctor.state"),
        }):
            doctor = vpn_doctor.VpnDoctor(args(directory))
            with patch.object(doctor, "docker_running", return_value=True), \
                    patch.object(doctor, "namespace", return_value="net:[1]"), \
                    patch.object(doctor, "command") as command, \
                    patch.object(doctor, "vpn_health", return_value=(True, "ready")), \
                    patch.object(doctor, "host_isolated", return_value=(True, "isolated")), \
                    patch.object(doctor, "worker_request") as worker, \
                    patch.object(doctor, "compose_up") as compose:
                command.return_value.returncode = 0
                report = doctor.preflight()
            self.assertTrue(report.ok)
            worker.assert_not_called()
            compose.assert_not_called()

    def test_preflight_cache_reuses_one_inspection(self) -> None:
        with TemporaryDirectory() as directory, patch.dict(os.environ, {
            "BO_VPN_DOCTOR_LOG": str(Path(directory) / "doctor.log"),
            "BO_VPN_DOCTOR_STATE": str(Path(directory) / "doctor.state"),
            "BO_VPN_PREFLIGHT_CACHE": str(Path(directory) / "cache.json"),
            "BO_VPN_PREFLIGHT_LOCK": str(Path(directory) / "cache.lock"),
            "BO_VPN_PREFLIGHT_CACHE_TTL_SEC": "30",
        }):
            doctor = vpn_doctor.VpnDoctor(args(directory))
            expected = vpn_doctor.Report([vpn_doctor.Check("vpn", True, "ready")])
            with patch.object(doctor, "preflight", return_value=expected) as preflight:
                first, first_fresh = vpn_doctor.cached_preflight(doctor)
                second, second_fresh = vpn_doctor.cached_preflight(doctor)
            self.assertTrue(first.ok and second.ok)
            self.assertTrue(first_fresh)
            self.assertFalse(second_fresh)
            preflight.assert_called_once()

    def test_worker_post_sends_matching_request_id_header(self) -> None:
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            @staticmethod
            def read():
                return b'{"ok": true}'

        with TemporaryDirectory() as directory, patch.dict(os.environ, {
            "BO_VPN_DOCTOR_LOG": str(Path(directory) / "doctor.log"),
            "BO_VPN_DOCTOR_STATE": str(Path(directory) / "doctor.state"),
        }):
            doctor = vpn_doctor.VpnDoctor(args(directory))
            with patch.object(vpn_doctor.urllib.request, "urlopen", return_value=Response()) as urlopen:
                result = doctor.worker_request("POST", "/tasks", {"request_id": "doctor-request"})
            request = urlopen.call_args.args[0]
            headers = dict(request.header_items())
            self.assertEqual(headers["X-request-id"], "doctor-request")
            self.assertTrue(result["ok"])


if __name__ == "__main__":
    unittest.main()
