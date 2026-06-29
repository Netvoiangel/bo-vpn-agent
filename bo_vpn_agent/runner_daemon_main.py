from __future__ import annotations

import argparse

from .config import RunnerDaemonConfig
from .runner_daemon import run_runner_daemon


def main() -> None:
    parser = argparse.ArgumentParser(description="BO/VPN runner-daemon skeleton")
    parser.add_argument("--host", default=None, help="HTTP bind host")
    parser.add_argument("--port", default=None, type=int, help="HTTP bind port")
    args = parser.parse_args()

    config = RunnerDaemonConfig.from_env()
    if args.host is not None:
        config.host = args.host
    if args.port is not None:
        config.port = args.port
    run_runner_daemon(config)


if __name__ == "__main__":
    main()
