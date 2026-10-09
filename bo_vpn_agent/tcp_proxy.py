from __future__ import annotations

import ipaddress
import logging
import os
import selectors
import socket
import threading


LOGGER = logging.getLogger("vpn-tcp-proxy")
BUFFER_SIZE = 64 * 1024


def relay(client: socket.socket, upstream: socket.socket) -> None:
    selector = selectors.DefaultSelector()
    selector.register(client, selectors.EVENT_READ, upstream)
    selector.register(upstream, selectors.EVENT_READ, client)
    try:
        while selector.get_map():
            for key, _mask in selector.select(timeout=30):
                source = key.fileobj
                destination = key.data
                data = source.recv(BUFFER_SIZE)
                if not data:
                    selector.unregister(source)
                    try:
                        destination.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    continue
                destination.sendall(data)
    finally:
        selector.close()


def handle(client: socket.socket, address: tuple[str, int]) -> None:
    upstream: socket.socket | None = None
    try:
        client.settimeout(10)
        request = b""
        while b"\n" not in request and len(request) < 512:
            chunk = client.recv(512 - len(request))
            if not chunk:
                return
            request += chunk
        line, _separator, remainder = request.partition(b"\n")
        parts = line.decode("ascii").strip().split()
        if len(parts) != 3 or parts[0] != "CONNECT":
            raise ValueError("expected CONNECT <ip> <port>")
        address_value = ipaddress.ip_address(parts[1])
        if address_value not in ipaddress.ip_network("172.26.0.0/15"):
            raise ValueError("destination is outside 172.26.0.0/15")
        host = str(address_value)
        port = int(parts[2])
        upstream = socket.create_connection((host, port), timeout=15)
        client.sendall(b"OK\n")
        if remainder:
            upstream.sendall(remainder)
        client.settimeout(None)
        upstream.settimeout(None)
        LOGGER.info("connected client=%s destination=%s:%s", address[0], host, port)
        relay(client, upstream)
    except Exception as exc:  # noqa: BLE001 - return compact proxy error to client
        LOGGER.warning("proxy request failed client=%s error=%s", address[0], exc)
        try:
            client.sendall(f"ERR {exc}\n".encode("utf-8"))
        except OSError:
            pass
    finally:
        client.close()
        if upstream is not None:
            upstream.close()


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    host = os.getenv("VPN_TCP_PROXY_HOST", "0.0.0.0")
    port = int(os.getenv("VPN_TCP_PROXY_PORT", "18080"))
    with socket.create_server((host, port), reuse_port=False) as server:
        LOGGER.info("vpn tcp proxy listening on %s:%s", host, port)
        while True:
            client, address = server.accept()
            threading.Thread(target=handle, args=(client, address), daemon=True).start()


if __name__ == "__main__":
    main()
