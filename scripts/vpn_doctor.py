#!/usr/bin/env python3
"""Diagnose and repair the containerized UniVPN path without logging secrets."""

from __future__ import annotations

import argparse
import fcntl
import ipaddress
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SENSITIVE = re.compile(r"(?i)(password|username|credential|authorization|bearer|secret|token)")
TOKEN_URL = re.compile(r"/bot[0-9]+:[A-Za-z0-9_-]+/")


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)

    def add(self, name: str, ok: bool, detail: str) -> None:
        self.checks.append(Check(name, ok, detail))

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checks": [{"name": item.name, "ok": item.ok, "detail": item.detail} for item in self.checks],
            "actions": self.actions,
        }


class DoctorError(RuntimeError):
    pass


class VpnDoctor:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.repo = Path(args.repo).resolve()
        self.compose = self.repo / args.compose_file
        self.worker_url = os.getenv("BO_VPN_DOCTOR_WORKER_URL", "http://127.0.0.1:8000").rstrip("/")
        self.worker_token = os.getenv("BO_VPN_DOCTOR_WORKER_TOKEN", "dev-token")
        self.log_path = Path(os.getenv("BO_VPN_DOCTOR_LOG", "/home/timur/univpn/logs/vpn-doctor.log"))
        self.state_path = Path(os.getenv("BO_VPN_DOCTOR_STATE", "/home/timur/univpn/logs/vpn-doctor.state"))
        self.max_log_bytes = int(os.getenv("BO_VPN_DOCTOR_LOG_MAX_BYTES", "1048576"))
        self.log_backups = max(1, int(os.getenv("BO_VPN_DOCTOR_LOG_BACKUPS", "2")))

    def command(self, *command: str, timeout: int = 30, check: bool = True) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(command, text=True, capture_output=True, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DoctorError(f"command failed: {command[0]}: {type(exc).__name__}") from exc
        if check and result.returncode != 0:
            detail = self.clean(result.stderr or result.stdout or f"exit {result.returncode}")
            raise DoctorError(f"{command[0]} failed: {detail[:300]}")
        return result

    @staticmethod
    def clean(value: str) -> str:
        lines = []
        for raw in value.replace("\r", "").splitlines():
            line = TOKEN_URL.sub("/bot[REDACTED]/", raw)
            lines.append("[redacted sensitive log line]" if SENSITIVE.search(line) else line)
        return " | ".join(lines).strip()

    def rotate(self) -> None:
        try:
            if not self.log_path.exists() or self.log_path.stat().st_size < self.max_log_bytes:
                return
            oldest = self.log_path.with_name(self.log_path.name + f".{self.log_backups}")
            oldest.unlink(missing_ok=True)
            for index in range(self.log_backups - 1, 0, -1):
                source = self.log_path.with_name(self.log_path.name + f".{index}")
                if source.exists():
                    source.replace(self.log_path.with_name(self.log_path.name + f".{index + 1}"))
            self.log_path.replace(self.log_path.with_name(self.log_path.name + ".1"))
        except OSError:
            pass

    def log(self, level: str, event: str, detail: str = "") -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.rotate()
        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        line = f"{timestamp} level={level} event={event}"
        if detail:
            line += f" detail={self.clean(detail)}"
        with self.log_path.open("a", encoding="utf-8") as destination:
            destination.write(line + "\n")

    def docker_running(self, name: str) -> bool:
        result = self.command("docker", "inspect", "-f", "{{.State.Running}}", name, check=False)
        return result.returncode == 0 and result.stdout.strip() == "true"

    def namespace(self, name: str) -> str | None:
        result = self.command("docker", "exec", name, "readlink", "/proc/self/ns/net", check=False)
        return result.stdout.strip() if result.returncode == 0 else None

    def worker_health(self) -> tuple[bool, str]:
        try:
            result = self.worker_request("GET", "/health", timeout=5)
            return bool(result.get("ok")), str(result.get("status") or "unknown")
        except DoctorError as exc:
            return False, str(exc)

    def runner_health(self) -> tuple[bool, str]:
        code = (
            'import os,urllib.request; '
            'print(urllib.request.urlopen(os.environ["BO_VPN_RUNNER_URL"]+"/health",timeout=5).read().decode())'
        )
        result = self.command("docker", "exec", "bo-vpn-worker", "python", "-c", code, timeout=8, check=False)
        if result.returncode != 0:
            return False, self.clean(result.stderr or "runner unavailable")
        try:
            payload = json.loads(result.stdout)
            return bool(payload.get("ok")), str(payload.get("status") or "unknown")
        except ValueError:
            return False, "invalid runner health response"

    def vpn_health(self) -> tuple[bool, str]:
        interface = self.command("docker", "exec", "univpn-service", "ip", "-o", "addr", "show", "dev", "cnem_vnic", check=False)
        route = self.command("docker", "exec", "univpn-service", "ip", "route", "show", "172.26.0.0/15", check=False)
        ok = interface.returncode == 0 and bool(interface.stdout.strip()) and bool(route.stdout.strip())
        return ok, "cnem_vnic and 172.26.0.0/15 route present" if ok else "cnem_vnic or route missing"

    def host_isolated(self) -> tuple[bool, str]:
        interface = self.command("ip", "link", "show", "cnem_vnic", check=False)
        route = self.command("ip", "route", "show", "172.26.0.0/15", check=False)
        ok = interface.returncode != 0 and not route.stdout.strip()
        return ok, "VPN route absent on host" if ok else "VPN interface or route leaked to host"

    def inspect(self) -> Report:
        report = Report()
        for name in ("univpn-service", "bo-vpn-runner", "bo-vpn-worker"):
            running = self.docker_running(name)
            report.add(f"container:{name}", running, "running" if running else "not running")
        proxy_exists = self.command("docker", "inspect", "bo-vpn-tcp-proxy", check=False).returncode == 0
        if proxy_exists:
            proxy_running = self.docker_running("bo-vpn-tcp-proxy")
            report.add("container:bo-vpn-tcp-proxy", proxy_running, "running" if proxy_running else "not running")
        vpn_ns = self.namespace("univpn-service")
        for name in ("bo-vpn-runner", "bo-vpn-tcp-proxy"):
            if name == "bo-vpn-tcp-proxy" and not proxy_exists:
                continue
            own_ns = self.namespace(name)
            report.add(f"namespace:{name}", bool(vpn_ns and own_ns == vpn_ns),
                       "matches UniVPN" if vpn_ns and own_ns == vpn_ns else "stale or unavailable")
        worker_ok, worker_detail = self.worker_health()
        report.add("worker", worker_ok, worker_detail)
        runner_ok, runner_detail = self.runner_health() if worker_ok else (False, "worker unavailable")
        report.add("runner", runner_ok, runner_detail)
        vpn_ok, vpn_detail = self.vpn_health() if self.docker_running("univpn-service") else (False, "container unavailable")
        report.add("vpn", vpn_ok, vpn_detail)
        isolated, isolation_detail = self.host_isolated()
        report.add("host_isolation", isolated, isolation_detail)
        return report

    def preflight(self) -> Report:
        """Check only the transport needed by vpn_ssh; never call worker or compose."""
        report = Report()
        vpn_running = self.docker_running("univpn-service")
        runner_running = self.docker_running("bo-vpn-runner")
        report.add("container:univpn-service", vpn_running, "running" if vpn_running else "not running")
        report.add("container:bo-vpn-runner", runner_running, "running" if runner_running else "not running")
        vpn_ns = self.namespace("univpn-service") if vpn_running else None
        runner_ns = self.namespace("bo-vpn-runner") if runner_running else None
        report.add("namespace:bo-vpn-runner", bool(vpn_ns and runner_ns == vpn_ns),
                   "matches UniVPN" if vpn_ns and runner_ns == vpn_ns else "stale or unavailable")
        if runner_running:
            key = self.command("docker", "exec", "bo-vpn-runner", "test", "-r", "/run/keys/rsa.key", check=False)
            report.add("runner:ssh-key", key.returncode == 0, "readable" if key.returncode == 0 else "not readable")
        else:
            report.add("runner:ssh-key", False, "runner unavailable")
        vpn_ok, vpn_detail = self.vpn_health() if vpn_running else (False, "container unavailable")
        report.add("vpn", vpn_ok, vpn_detail)
        isolated, isolation_detail = self.host_isolated()
        report.add("host_isolation", isolated, isolation_detail)
        return report

    def compose_up(self, *services: str, force: bool = False) -> None:
        command = ["docker", "compose", "-f", str(self.compose), "--profile", "audio", "up", "-d", "--no-deps"]
        if force:
            command.append("--force-recreate")
        command.extend(services)
        self.command(*command, timeout=120)

    @staticmethod
    def wait_until(check: Any, timeout: int = 20) -> tuple[bool, str]:
        deadline = time.monotonic() + timeout
        result: tuple[bool, str] = (False, "not ready")
        while time.monotonic() < deadline:
            result = check()
            if result[0]:
                return result
            time.sleep(1)
        return result

    def worker_request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        timeout: int = 10,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        body = json.dumps(payload).encode() if payload is not None else None
        headers = {"Authorization": f"Bearer {self.worker_token}", "Content-Type": "application/json"}
        effective_request_id = request_id or (str(payload["request_id"]) if payload and payload.get("request_id") else None)
        if effective_request_id:
            headers["X-Request-Id"] = effective_request_id
        request = urllib.request.Request(
            self.worker_url + path,
            data=body,
            method=method,
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            try:
                error = json.loads(exc.read().decode())
                nested = error.get("error")
                detail = error.get("error_code") or (nested.get("error_code") if isinstance(nested, dict) else nested)
                detail = detail or f"HTTP {exc.code}"
            except (ValueError, AttributeError, TypeError):
                detail = f"HTTP {exc.code}"
            raise DoctorError(f"worker request failed: {detail}") from exc
        except (OSError, ValueError) as exc:
            raise DoctorError(f"worker request failed: {type(exc).__name__}") from exc

    def trigger_login(self, canary_ip: str) -> str:
        address = ipaddress.ip_address(canary_ip)
        if address.version != 4 or address not in ipaddress.ip_network("172.26.0.0/15"):
            raise DoctorError("canary IP must be inside 172.26.0.0/15")
        request_id = f"vpn-doctor-{uuid.uuid4()}"
        task = self.worker_request("POST", "/tasks", {
            "request_id": request_id,
            "telegram_user_id": 0,
            "user_role": "engineer",
            "vehicle": {"number": "vpn-doctor-canary", "ip": str(address)},
            "vpn": {"mode": "container_secret"},
            "operation": "vehicle_reachability",
            "params": {},
            "runner_mode": "container_namespace",
            "timeout_sec": 90,
        })
        task_id = str(task["task_id"])
        deadline = time.monotonic() + 100
        while time.monotonic() < deadline:
            result = self.worker_request("GET", f"/tasks/{task_id}", timeout=10, request_id=request_id)
            state = str(result.get("state"))
            if state in {"finished", "failed", "timeout"}:
                return f"task={state} code={result.get('error_code') or 'none'}"
            time.sleep(2)
        raise DoctorError("canary task polling timed out")

    def capture_failure_context(self) -> None:
        for name in ("bo-vpn-runner", "bo-vpn-worker", "bo-vpn-tcp-proxy"):
            if not self.docker_running(name):
                continue
            result = self.command("docker", "logs", "--since", "15m", "--tail", "60", name, check=False)
            cleaned = self.clean((result.stdout + "\n" + result.stderr)[-24000:])
            if cleaned:
                self.log("ERROR", f"context:{name}", cleaned)
        # UniVPN console output is intentionally never collected because it may echo credentials.

    def repair(self, canary_ip: str | None) -> Report:
        if not self.compose.exists():
            raise DoctorError(f"compose file not found: {self.compose}")
        report = self.inspect()
        if not self.docker_running("univpn-service"):
            self.log("ACTION", "start-univpn")
            self.compose_up("univpn-service")
            report.actions.append("started univpn-service")
        if not self.docker_running("bo-vpn-worker"):
            self.log("ACTION", "start-worker")
            self.compose_up("bo-vpn-worker")
            report.actions.append("started bo-vpn-worker")
            self.wait_until(self.worker_health)

        vpn_ns = self.namespace("univpn-service")
        sidecars = ["bo-vpn-runner"]
        if self.command("docker", "inspect", "bo-vpn-tcp-proxy", check=False).returncode == 0:
            sidecars.append("vpn-tcp-proxy")
        stale = [name for name in sidecars if self.namespace("bo-vpn-tcp-proxy" if name == "vpn-tcp-proxy" else name) != vpn_ns]
        if stale or not self.docker_running("bo-vpn-runner"):
            health_ok, health_status = self.worker_health()
            if health_ok and health_status == "busy":
                raise DoctorError("worker is busy; namespace repair deferred")
            self.log("ACTION", "recreate-sidecars", ",".join(stale or ["bo-vpn-runner"]))
            self.compose_up(*sidecars, force=True)
            report.actions.append("recreated VPN sidecars")
            self.wait_until(self.runner_health)

        worker_ok, worker_status = self.worker_health()
        if not worker_ok:
            raise DoctorError(f"worker unavailable after repair: {worker_status}")
        runner_ok, runner_status = self.runner_health()
        if not runner_ok:
            self.log("ACTION", "recreate-runner", runner_status)
            self.compose_up("bo-vpn-runner", force=True)
            report.actions.append("recreated runner")
            runner_ok, runner_status = self.wait_until(self.runner_health)
            if not runner_ok:
                raise DoctorError(f"runner unavailable after recreate: {runner_status}")

        vpn_ok, _ = self.vpn_health()
        if not vpn_ok:
            if worker_status == "busy":
                raise DoctorError("worker is busy; VPN login deferred")
            if not canary_ip:
                raise DoctorError("VPN is disconnected; pass --canary-ip or BO_VPN_DOCTOR_CANARY_IP")
            self.log("ACTION", "trigger-managed-login")
            detail = self.trigger_login(canary_ip)
            report.actions.append(f"triggered managed login ({detail})")
            vpn_ok, _ = self.vpn_health()
            if not vpn_ok:
                self.log("ACTION", "reset-vpn-container", "managed login did not establish route")
                self.compose_up("univpn-service", force=True)
                self.compose_up(*sidecars, force=True)
                report.actions.append("reset UniVPN container after failed login")
                runner_ok, runner_status = self.wait_until(self.runner_health)
                if not runner_ok:
                    raise DoctorError(f"runner unavailable after VPN reset: {runner_status}")
                detail = self.trigger_login(canary_ip)
                report.actions.append(f"retried managed login ({detail})")
        final = self.inspect()
        final.actions = report.actions
        return final

    def record_state(self, report: Report, error: str | None = None) -> None:
        failed = sorted(item.name for item in report.checks if not item.ok)
        state = "healthy" if report.ok and not error else "error:" + ",".join(failed or [error or "unknown"])
        try:
            previous = self.state_path.read_text(encoding="utf-8").strip()
        except OSError:
            previous = ""
        if state != previous:
            self.log("INFO" if state == "healthy" else "ERROR", "state-change", state)
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(state + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    default_repo = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="Diagnose or repair the bo-vpn container path")
    parser.add_argument("mode", choices=("preflight", "status", "repair"), nargs="?", default="status")
    parser.add_argument("--canary-ip", default=os.getenv("BO_VPN_DOCTOR_CANARY_IP"))
    parser.add_argument("--repo", default=os.getenv("BO_VPN_DOCTOR_REPO", str(default_repo)))
    parser.add_argument("--compose-file", default=os.getenv("BO_VPN_DOCTOR_COMPOSE_FILE", "docker-compose.full.yml"))
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    return parser.parse_args()


def report_from_dict(payload: dict[str, Any]) -> Report:
    return Report(
        checks=[Check(str(item["name"]), bool(item["ok"]), str(item["detail"]))
                for item in payload.get("checks", [])],
        actions=[str(item) for item in payload.get("actions", [])],
    )


def cached_preflight(doctor: VpnDoctor) -> tuple[Report, bool]:
    cache_path = Path(os.getenv("BO_VPN_PREFLIGHT_CACHE", "/tmp/bo-vpn-preflight.json"))
    lock_path = Path(os.getenv("BO_VPN_PREFLIGHT_LOCK", "/tmp/bo-vpn-preflight.lock"))
    ttl = max(0.0, float(os.getenv("BO_VPN_PREFLIGHT_CACHE_TTL_SEC", "5")))
    with lock_path.open("w") as lock:
        deadline = time.monotonic() + 15
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise DoctorError("preflight cache lock timed out")
                time.sleep(0.05)
        try:
            if cache_path.exists() and time.time() - cache_path.stat().st_mtime <= ttl:
                payload = json.loads(cache_path.read_text(encoding="utf-8"))
                return report_from_dict(payload), False
        except (OSError, ValueError, KeyError, TypeError):
            pass
        report = doctor.preflight()
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=cache_path.parent,
                                             prefix=".vpn-preflight-", delete=False) as destination:
                temporary = Path(destination.name)
                json.dump(report.as_dict(), destination, ensure_ascii=True)
            temporary.replace(cache_path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return report, True


def print_report(report: Report, as_json: bool) -> None:
    if as_json:
        print(json.dumps(report.as_dict(), ensure_ascii=False))
        return
    for check in report.checks:
        print(f"{'OK' if check.ok else 'FAIL':4} {check.name:34} {check.detail}")
    for action in report.actions:
        print(f"DONE {'action':34} {action}")
    print("VPN HEALTHY" if report.ok else "VPN DEGRADED")


def main() -> int:
    args = parse_args()
    doctor = VpnDoctor(args)
    if args.mode == "preflight":
        try:
            report, fresh = cached_preflight(doctor)
            if fresh:
                doctor.record_state(report)
            if not args.quiet:
                print_report(report, args.json)
            return 0 if report.ok else 1
        except (DoctorError, OSError) as exc:
            if not args.quiet:
                print(f"VPN PREFLIGHT FAILED: {doctor.clean(str(exc))}", file=sys.stderr)
            return 1
    if args.mode == "status":
        try:
            report = doctor.inspect()
            doctor.record_state(report)
            if not args.quiet:
                print_report(report, args.json)
            return 0 if report.ok else 1
        except (DoctorError, OSError) as exc:
            if not args.quiet:
                print(f"VPN STATUS FAILED: {doctor.clean(str(exc))}", file=sys.stderr)
            return 1

    lock_path = Path(os.getenv("BO_VPN_REPAIR_LOCK", "/tmp/bo-vpn-repair.lock"))
    with lock_path.open("w") as lock:
        deadline = time.monotonic() + 120
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    print("VPN doctor is already running", file=sys.stderr)
                    return 75
                time.sleep(1)
        report = Report()
        try:
            report = doctor.repair(args.canary_ip)
            Path(os.getenv("BO_VPN_PREFLIGHT_CACHE", "/tmp/bo-vpn-preflight.json")).unlink(missing_ok=True)
            doctor.record_state(report)
            if not args.quiet:
                print_report(report, args.json)
            return 0 if report.ok else 1
        except (DoctorError, OSError) as exc:
            message = doctor.clean(str(exc))
            doctor.log("ERROR", "repair-failed", message)
            doctor.capture_failure_context()
            doctor.record_state(report, message)
            if not args.quiet:
                print(f"VPN REPAIR FAILED: {message}", file=sys.stderr)
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
