#!/usr/bin/env python3
from __future__ import annotations

import os
import selectors
import socket
import sys


def read_line(sock: socket.socket) -> bytes:
    result = b""
    while not result.endswith(b"\n") and len(result) < 1024:
        chunk = sock.recv(1)
        if not chunk:
            break
        result += chunk
    return result


def main() -> None:
    if len(sys.argv) != 5:
        raise SystemExit("usage: proxy_connect.py <proxy-host> <proxy-port> <target-host> <target-port>")
    proxy_host, proxy_port, target_host, target_port = sys.argv[1:]
    sock = socket.create_connection((proxy_host, int(proxy_port)), timeout=15)
    sock.sendall(f"CONNECT {target_host} {target_port}\n".encode("ascii"))
    response = read_line(sock)
    if response != b"OK\n":
        raise SystemExit(response.decode("utf-8", errors="replace").strip() or "proxy rejected connection")

    sock.setblocking(False)
    selector = selectors.DefaultSelector()
    selector.register(sock, selectors.EVENT_READ, "socket")
    selector.register(sys.stdin.buffer, selectors.EVENT_READ, "stdin")
    try:
        while selector.get_map():
            for key, _mask in selector.select():
                if key.data == "stdin":
                    data = os.read(sys.stdin.fileno(), 64 * 1024)
                    if data:
                        sock.sendall(data)
                    else:
                        selector.unregister(sys.stdin.buffer)
                        sock.shutdown(socket.SHUT_WR)
                else:
                    try:
                        data = sock.recv(64 * 1024)
                    except BlockingIOError:
                        continue
                    if not data:
                        return
                    view = memoryview(data)
                    while view:
                        written = os.write(sys.stdout.fileno(), view)
                        view = view[written:]
    finally:
        selector.close()
        sock.close()


if __name__ == "__main__":
    main()
