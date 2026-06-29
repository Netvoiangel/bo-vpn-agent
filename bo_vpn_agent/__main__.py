from __future__ import annotations

import argparse

from .api import run_server
from .config import WorkerConfig


def main() -> None:
    parser = argparse.ArgumentParser(description="BO/VPN diagnostic worker")
    parser.add_argument("--host", default=None, help="HTTP bind host")
    parser.add_argument("--port", default=None, type=int, help="HTTP bind port")
    args = parser.parse_args()

    config = WorkerConfig.from_env()
    if args.host is not None:
        config.host = args.host
    if args.port is not None:
        config.port = args.port
    run_server(config)


if __name__ == "__main__":
    main()
