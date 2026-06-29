from __future__ import annotations

import json
from dataclasses import dataclass

from .command_exec import CommandExecutor, CommandResult, SubprocessCommandExecutor
from .config import RunnerDaemonConfig
from .models import Task
from .runner import RunnerFailure, RunnerResult


TCP_PORTS = (22, 443, 80)


@dataclass(slots=True)
class ExistingContainerExecutor:
    config: RunnerDaemonConfig
    command_executor: CommandExecutor

    @classmethod
    def from_config(cls, config: RunnerDaemonConfig) -> "ExistingContainerExecutor":
        return cls(config=config, command_executor=SubprocessCommandExecutor())

    def run(self, task: Task) -> RunnerResult:
        container_pid = self._container_pid()
        if task.operation == "vehicle_reachability":
            return self._vehicle_reachability(container_pid, task)
        if task.operation == "basic_status":
            return self._basic_status(container_pid, task)
        raise RunnerFailure("operation_not_allowed", "Операция пока не реализована для existing_container")

    def _container_pid(self) -> str:
        result = self.command_executor.run(
            [
                self.config.docker_bin,
                "inspect",
                "-f",
                "{{.State.Pid}}",
                self.config.existing_container_name,
            ],
            timeout_sec=self.config.nsenter_timeout_sec,
        )
        if result.timed_out:
            raise RunnerFailure("operation_timeout", "docker inspect превысил timeout")
        pid = result.stdout.strip()
        if result.returncode != 0 or not pid or pid == "0":
            raise RunnerFailure("vpn_client_error", "Не удалось получить PID existing UniVPN container")
        return pid

    def _vehicle_reachability(self, container_pid: str, task: Task) -> RunnerResult:
        payload = {
            "host": task.vehicle.ip,
            "ports": list(TCP_PORTS),
            "timeout": min(self.config.nsenter_timeout_sec, task.timeout_sec),
        }
        script = _tcp_probe_script(payload)
        result = self._nsenter(container_pid, ["python3", "-c", script], timeout_sec=task.timeout_sec)
        if result.timed_out:
            raise RunnerFailure("operation_timeout", "TCP-проверка через nsenter превысила timeout")
        if result.returncode != 0:
            raise RunnerFailure("vpn_client_error", "TCP-проверка через nsenter завершилась ошибкой")
        try:
            probe = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RunnerFailure("vpn_client_error", "TCP-проверка вернула некорректный JSON") from exc

        tcp = {f"tcp_{port}": str(probe.get(str(port), "closed")) for port in TCP_PORTS}
        if tcp["tcp_22"] != "open" and tcp["tcp_443"] != "open":
            raise RunnerFailure("vehicle_unreachable", "ТС недоступно по 22/tcp и 443/tcp")
        return RunnerResult(
            summary="ТС доступно через existing UniVPN namespace",
            data=tcp,
        )

    def _basic_status(self, container_pid: str, task: Task) -> RunnerResult:
        ssh_command = "hostname; uptime; date -Is; df -h /; free -m"
        result = self._nsenter(
            container_pid,
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
            raise RunnerFailure("operation_timeout", "SSH basic_status через nsenter превысил timeout")
        if result.returncode != 0:
            raise RunnerFailure("ssh_failed", "SSH basic_status через nsenter завершился ошибкой")
        return RunnerResult(
            summary="Базовый статус ТС получен через SSH",
            data=_parse_basic_status(result.stdout),
        )

    def _nsenter(self, container_pid: str, command: list[str], timeout_sec: int) -> CommandResult:
        return self.command_executor.run(
            [
                self.config.nsenter_bin,
                "-t",
                container_pid,
                "-n",
                *command,
            ],
            timeout_sec=min(timeout_sec, self.config.nsenter_timeout_sec),
        )


def _tcp_probe_script(payload: dict[str, object]) -> str:
    encoded_payload = json.dumps(payload, separators=(",", ":"))
    return (
        "import json, socket; "
        f"payload=json.loads({encoded_payload!r}); "
        "out={}; "
        "host=payload['host']; "
        "timeout=float(payload['timeout']); "
        "\nfor port in payload['ports']:\n"
        "    sock=socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
        "    sock.settimeout(timeout)\n"
        "    try:\n"
        "        sock.connect((host, int(port)))\n"
        "        out[str(port)]='open'\n"
        "    except OSError:\n"
        "        out[str(port)]='closed'\n"
        "    finally:\n"
        "        sock.close()\n"
        "print(json.dumps(out, sort_keys=True))\n"
    )


def _parse_basic_status(stdout: str) -> dict[str, str]:
    lines = stdout.splitlines()
    disk_start = _find_line_index(lines, "Filesystem")
    memory_start = _find_line_index(lines, "total")
    return {
        "hostname": lines[0].strip() if len(lines) > 0 else "",
        "uptime_raw": lines[1].strip() if len(lines) > 1 else "",
        "system_time": lines[2].strip() if len(lines) > 2 else "",
        "disk_root_raw": "\n".join(lines[disk_start:memory_start]).strip() if disk_start is not None else "",
        "memory_raw": "\n".join(lines[memory_start:]).strip() if memory_start is not None else "",
    }


def _find_line_index(lines: list[str], prefix: str) -> int | None:
    for index, line in enumerate(lines):
        if line.lstrip().startswith(prefix):
            return index
    return None

