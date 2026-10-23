from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "init_univpn_profile.sh"


class InitUniVpnProfileScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp(prefix="univpn-init-test-")
        self.tmp_path = Path(self.tmp_dir)
        self.bin_dir = self.tmp_path / "bin"
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        self.mock_log = self.tmp_path / "mock_docker.log"
        self.profile_dir = self.tmp_path / "profile"
        self.profile_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _create_mock_docker(self, script_body: str) -> Path:
        docker_path = self.bin_dir / "docker"
        docker_path.write_text(
            f"#!/usr/bin/env bash\n"
            f"echo \"$@\" >> \"{self.mock_log}\"\n"
            f"{script_body}\n",
            encoding="utf-8",
        )
        docker_path.chmod(0o755)
        return docker_path

    def _run_script(
        self,
        args: list[str] | None = None,
        env_extra: dict[str, str] | None = None,
        stdin_data: str | None = None,
        timeout: float = 10.0,
    ) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env["PATH"] = f"{self.bin_dir}:{env.get('PATH', '')}"
        if env_extra:
            env.update(env_extra)

        cmd = [str(SCRIPT_PATH), "--profile-dir", str(self.profile_dir)] + (args or [])
        return subprocess.run(
            cmd,
            input=stdin_data,
            text=True,
            capture_output=True,
            env=env,
            timeout=timeout,
        )

    def test_non_interactive_tty_rejection(self) -> None:
        """When run without a real TTY, the script must fail-safe to prevent EOF infinite loops."""
        self._create_mock_docker("exit 0")
        proc = self._run_script(stdin_data="")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("UniVPNCS requires an interactive TTY", proc.stderr)
        self.assertIn("Automated piping or non-interactive background execution is disabled", proc.stderr)

    def test_docker_run_receives_logging_and_safety_flags(self) -> None:
        """Verify that docker run is called with log limits (10m, 3 files), --name, and --stop-timeout."""
        mock_body = f"""
if [ "$1" = "image" ]; then
    exit 0
fi
if [ "$1" = "run" ]; then
    mkdir -p "{self.profile_dir}"
    echo "[Session0]" > "{self.profile_dir}/sysconfig.ini"
    exit 0
fi
exit 0
"""
        self._create_mock_docker(mock_body)
        proc = self._run_script(
            args=["--image", "test/univpn:latest"],
            env_extra={"INIT_UNIVPN_FORCE_NONINTERACTIVE": "1"},
        )
        self.assertEqual(proc.returncode, 0, f"Script failed with output: {proc.stdout}\n{proc.stderr}")
        self.assertIn("SUCCESS: Профиль подключения UniVPN успешно сохранён!", proc.stdout)

        log_content = self.mock_log.read_text(encoding="utf-8")
        self.assertIn("--log-opt max-size=10m", log_content)
        self.assertIn("--log-opt max-file=3", log_content)
        self.assertIn("--rm", log_content)
        self.assertIn("--stop-timeout 5", log_content)
        self.assertIn("--name univpn-init-", log_content)

    def test_missing_profile_fails_without_false_positive(self) -> None:
        """If docker exits cleanly but sysconfig.ini was NOT created, the script must fail with code 1."""
        mock_body = """
if [ "$1" = "image" ]; then
    exit 0
fi
# Exits without creating sysconfig.ini
exit 0
"""
        self._create_mock_docker(mock_body)
        proc = self._run_script(
            args=["--image", "test/univpn:latest"],
            env_extra={"INIT_UNIVPN_FORCE_NONINTERACTIVE": "1"},
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("ERROR: Профиль не был создан", proc.stderr)
        self.assertNotIn("SUCCESS", proc.stdout)

    def test_empty_profile_fails(self) -> None:
        """If sysconfig.ini is empty (0 bytes), it should be treated as failed initialization."""
        mock_body = f"""
if [ "$1" = "image" ]; then
    exit 0
fi
if [ "$1" = "run" ]; then
    mkdir -p "{self.profile_dir}"
    touch "{self.profile_dir}/sysconfig.ini"
    exit 0
fi
exit 0
"""
        self._create_mock_docker(mock_body)
        proc = self._run_script(
            args=["--image", "test/univpn:latest"],
            env_extra={"INIT_UNIVPN_FORCE_NONINTERACTIVE": "1"},
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("ERROR: Профиль не был создан", proc.stderr)

    def test_timeout_kills_temporary_container(self) -> None:
        """When execution exceeds the timeout, the script aborts and invokes docker rm -f."""
        container_state = self.tmp_path / "container_running"
        mock_body = f"""
if [ "$1" = "image" ]; then
    exit 0
fi
if [ "$1" = "ps" ]; then
    if [ -f "{container_state}" ]; then
        cat "{container_state}"
    fi
    exit 0
fi
if [ "$1" = "rm" ]; then
    rm -f "{container_state}"
    exit 0
fi
if [ "$1" = "run" ]; then
    for arg in "$@"; do
        if [[ "$arg" == univpn-init-* ]]; then
            echo "$arg" > "{container_state}"
        fi
    done
    while [ -f "{container_state}" ]; do
        sleep 0.05
    done
    exit 0
fi
exit 0
"""
        self._create_mock_docker(mock_body)
        start = time.monotonic()
        proc = self._run_script(
            args=["--image", "test/univpn:latest", "--timeout", "1"],
            env_extra={"INIT_UNIVPN_FORCE_NONINTERACTIVE": "1"},
            timeout=8.0,
        )
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 4.0, "Timeout was not enforced in a timely manner")
        self.assertNotEqual(proc.returncode, 0)

        log_content = self.mock_log.read_text(encoding="utf-8")
        self.assertIn("rm -f univpn-init-", log_content)

    def test_cleanup_trap_on_signal(self) -> None:
        """If the script process receives SIGTERM, the bash trap must execute docker rm -f."""
        container_state = self.tmp_path / "container_running"
        mock_body = f"""
if [ "$1" = "image" ]; then
    exit 0
fi
if [ "$1" = "ps" ]; then
    if [ -f "{container_state}" ]; then
        cat "{container_state}"
    fi
    exit 0
fi
if [ "$1" = "rm" ]; then
    rm -f "{container_state}"
    exit 0
fi
if [ "$1" = "run" ]; then
    for arg in "$@"; do
        if [[ "$arg" == univpn-init-* ]]; then
            echo "$arg" > "{container_state}"
        fi
    done
    while [ -f "{container_state}" ]; do
        sleep 0.05
    done
    exit 0
fi
exit 0
"""
        self._create_mock_docker(mock_body)
        env = dict(os.environ)
        env["PATH"] = f"{self.bin_dir}:{env.get('PATH', '')}"
        env["INIT_UNIVPN_FORCE_NONINTERACTIVE"] = "1"

        proc = subprocess.Popen(
            [str(SCRIPT_PATH), "--profile-dir", str(self.profile_dir), "--image", "test/univpn:latest"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )

        try:
            # Wait until mock docker records container_state
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and not container_state.exists():
                time.sleep(0.05)

            self.assertTrue(container_state.exists(), "Container was not launched")

            # Send SIGTERM to the process group
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=5.0)

            log_content = self.mock_log.read_text(encoding="utf-8")
            self.assertIn("rm -f univpn-init-", log_content)
        finally:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    pass
            if proc.stdout:
                proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()


if __name__ == "__main__":
    unittest.main()
