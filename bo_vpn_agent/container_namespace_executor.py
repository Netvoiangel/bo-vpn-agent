from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass

from .command_exec import CommandExecutor, CommandResult, SubprocessCommandExecutor
from .config import RunnerDaemonConfig
from .existing_container_executor import TCP_PORTS, _parse_basic_status, _tcp_probe_script
from .models import Task, VpnCredentials
from .runner import RunnerFailure, RunnerResult, StateCallback


LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class ContainerNamespaceExecutor:
    config: RunnerDaemonConfig
    command_executor: CommandExecutor

    @classmethod
    def from_config(cls, config: RunnerDaemonConfig) -> "ContainerNamespaceExecutor":
        return cls(config=config, command_executor=SubprocessCommandExecutor(config.command_output_max_bytes))

    def run(self, task: Task, vpn: VpnCredentials, set_state: StateCallback) -> RunnerResult:
        warnings: list[str] = []
        operation_result: RunnerResult | None = None
        try:
            set_state("starting_vpn", "Проверка UniVPN session в container namespace")
            self._ensure_vpn_connected(vpn)
            set_state("vpn_connected", "UniVPN session доступна в container namespace")

            if task.operation == "vehicle_reachability":
                set_state("checking_vehicle", "Проверка TCP-доступности ТС")
                operation_result = self._vehicle_reachability(task)
            elif task.operation == "basic_status":
                set_state("checking_vehicle", "Проверка VPN-доступности перед SSH")
                set_state("running_operation", "Получение basic_status по SSH")
                operation_result = self._basic_status(task)
            else:
                raise RunnerFailure("operation_not_allowed", "Операция пока не реализована для container_namespace")

            warnings.extend(operation_result.warnings or [])
            return RunnerResult(
                summary=operation_result.summary,
                data=operation_result.data,
                artifacts=operation_result.artifacts,
                warnings=warnings,
            )
        finally:
            cleanup_warnings = self._cleanup(set_state)
            if operation_result is not None:
                warnings.extend(cleanup_warnings)

    def _ensure_vpn_connected(self, vpn: VpnCredentials) -> None:
        if self._is_connected(log_attempt=True, attempt=0):
            return
        if not self.config.manage_vpn_session:
            raise RunnerFailure(
                "vpn_client_error",
                f"VPN preflight: интерфейс {self.config.univpn_interface} или маршрут {self.config.univpn_route_cidr} отсутствует",
            )
        self._login(vpn)
        deadline = time.monotonic() + self.config.univpn_login_timeout_sec
        attempt = 1
        LOGGER.info("univpn login: waiting for interface and route")
        while True:
            if self._is_connected(log_attempt=True, attempt=attempt):
                return
            if time.monotonic() >= deadline:
                raise RunnerFailure(
                    "vpn_client_error",
                    f"UniVPN login не дал интерфейс {self.config.univpn_interface} и маршрут {self.config.univpn_route_cidr}",
                )
            _sleep(self.config.univpn_connect_poll_interval_sec)
            attempt += 1

    def _is_connected(self, log_attempt: bool = False, attempt: int | None = None) -> bool:
        interface_found = False
        route_found = False
        addr = self.command_executor.run(
            ["ip", "-br", "addr", "show", self.config.univpn_interface],
            timeout_sec=self.config.nsenter_timeout_sec,
        )
        if addr.timed_out:
            raise RunnerFailure("operation_timeout", f"VPN preflight: проверка {self.config.univpn_interface} превысила timeout")
        if addr.returncode == 0 and self.config.univpn_interface in addr.stdout:
            interface_found = True
        if not interface_found:
            if log_attempt:
                LOGGER.info(
                    "univpn preflight: poll attempt=%s interface_found=%s route_found=%s",
                    attempt,
                    interface_found,
                    route_found,
                )
            return False

        routes = self.command_executor.run(["ip", "route"], timeout_sec=self.config.nsenter_timeout_sec)
        if routes.timed_out:
            raise RunnerFailure("operation_timeout", "VPN preflight: проверка маршрутов превысила timeout")
        if routes.returncode != 0:
            raise RunnerFailure("vpn_client_error", "VPN preflight: не удалось получить маршруты")
        route_found = self.config.univpn_route_cidr in routes.stdout
        if log_attempt:
            LOGGER.info(
                "univpn preflight: poll attempt=%s interface_found=%s route_found=%s",
                attempt,
                interface_found,
                route_found,
            )
        return route_found

    def _login(self, vpn: VpnCredentials) -> None:
        username, password = self._credentials(vpn)
        LOGGER.info("univpn login: control_path_exists=%s", self.config.univpn_control_path.exists())
        try:
            self._write_control_line("3")
            LOGGER.info("univpn login: profile selected")
            _sleep(self.config.univpn_login_after_profile_delay_sec)

            self._write_control_line("1")
            LOGGER.info("univpn login: connect selected")
            _sleep(self.config.univpn_login_after_connect_delay_sec)

            self._write_control_line(username)
            LOGGER.info("univpn login: username submitted")
            _sleep(self.config.univpn_login_after_username_delay_sec)

            self._write_control_line(password)
            LOGGER.info("univpn login: password submitted")
            _sleep(self.config.univpn_post_login_wait_sec)
        except OSError as exc:
            raise RunnerFailure("vpn_client_error", "UniVPN control FIFO недоступен для managed login") from exc

    def _credentials(self, vpn: VpnCredentials) -> tuple[str, str]:
        if vpn.mode == "inline_once":
            if not vpn.username or not vpn.password:
                raise RunnerFailure("vpn_client_error", "UniVPN credentials отсутствуют")
            return vpn.username, vpn.password
        if vpn.mode != "container_secret" or self.config.univpn_login_mode != "container_secret":
            raise RunnerFailure("vpn_client_error", "UniVPN login mode не поддержан для container_namespace")
        try:
            secrets = _read_env_file(self.config.univpn_secret_path)
        except OSError as exc:
            raise RunnerFailure("vpn_client_error", "UniVPN secret file недоступен") from exc
        username = secrets.get("VPN_USERNAME", "")
        password = secrets.get("VPN_PASSWORD", "")
        if not username or not password:
            raise RunnerFailure("vpn_client_error", "UniVPN secret file не содержит VPN_USERNAME/VPN_PASSWORD")
        LOGGER.info("univpn login: secret keys found username=%s password=%s", bool(username), bool(password))
        return username, password

    def _vehicle_reachability(self, task: Task) -> RunnerResult:
        payload = {
            "host": task.vehicle.ip,
            "ports": list(TCP_PORTS),
            "timeout": min(self.config.nsenter_timeout_sec, task.timeout_sec),
        }
        result = self.command_executor.run(["python3", "-c", _tcp_probe_script(payload)], timeout_sec=task.timeout_sec)
        if result.timed_out:
            raise RunnerFailure("operation_timeout", "TCP-проверка в container namespace превысила timeout")
        if result.returncode != 0:
            raise RunnerFailure("vpn_client_error", "TCP-проверка в container namespace завершилась ошибкой")
        try:
            probe = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RunnerFailure("vpn_client_error", "TCP-проверка вернула некорректный JSON") from exc

        tcp = {f"tcp_{port}": str(probe.get(str(port), "closed")) for port in TCP_PORTS}
        if tcp["tcp_22"] != "open" and tcp["tcp_443"] != "open":
            raise RunnerFailure("vehicle_unreachable", "ТС недоступно по 22/tcp и 443/tcp")
        return RunnerResult(
            summary="ТС доступно через UniVPN container namespace",
            data=tcp,
        )

    def _basic_status(self, task: Task) -> RunnerResult:
        ssh_command = "hostname; uptime; date -Is; df -h /; free -m"
        result = self.command_executor.run(
            [
                self.config.ssh_bin,
                "-i",
                str(self.config.ssh_key_path),
                "-o",
                f"ConnectTimeout={self.config.nsenter_timeout_sec}",
                "-o",
                "StrictHostKeyChecking=no",
                f"{self.config.default_ssh_user}@{task.vehicle.ip}",
                ssh_command,
            ],
            timeout_sec=task.timeout_sec,
        )
        if result.timed_out:
            raise RunnerFailure("operation_timeout", "SSH basic_status в container namespace превысил timeout")
        if result.returncode != 0:
            raise RunnerFailure("ssh_failed", "SSH basic_status в container namespace завершился ошибкой")
        return RunnerResult(
            summary="Базовый статус ТС получен через SSH",
            data=_parse_basic_status(result.stdout),
        )

    def _cleanup(self, set_state: StateCallback) -> list[str]:
        if not self.config.stop_vpn_after_task:
            return []
        set_state("cleanup", "Cleanup UniVPN session")
        if not self.config.univpn_disconnect_sequence:
            return ["UniVPN cleanup requested, but disconnect sequence is not configured"]
        try:
            self._write_control_sequence(self.config.univpn_disconnect_sequence.splitlines())
        except OSError:
            return ["UniVPN cleanup failed"]
        return []

    def _write_control_sequence(self, lines: list[str]) -> None:
        payload = "\n".join(lines).rstrip("\n") + "\n"
        self.config.univpn_control_path.parent.mkdir(parents=True, exist_ok=True)
        with self.config.univpn_control_path.open("a", encoding="utf-8") as stream:
            stream.write(payload)

    def _write_control_line(self, line: str) -> None:
        self.config.univpn_control_path.parent.mkdir(parents=True, exist_ok=True)
        with self.config.univpn_control_path.open("a", encoding="utf-8") as stream:
            stream.write(f"{line}\n")
            stream.flush()


def _read_env_file(path: object) -> dict[str, str]:
    result: dict[str, str] = {}
    with open(path, encoding="utf-8") as stream:
        for raw_line in stream:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            result[key.strip()] = value.strip().strip('"').strip("'")
    return result


def _sleep(seconds: float) -> None:
    time.sleep(seconds)
