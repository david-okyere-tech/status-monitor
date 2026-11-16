"""Local services that exercise every check type without a public network."""

from __future__ import annotations

import random
import shutil
import socket
import ssl
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from status_monitor.models import TargetConfig


class DemoFixtures:
    def __init__(self) -> None:
        self.targets: list[TargetConfig] = []
        self._servers: list[ThreadingHTTPServer] = []
        self._sockets: list[socket.socket] = []

    def stop(self) -> None:
        for server in self._servers:
            server.shutdown()
            server.server_close()
        for sock in self._sockets:
            try:
                sock.close()
            except OSError:
                pass
        self._servers.clear()
        self._sockets.clear()


def start_fixtures(directory: Path | str) -> DemoFixtures:
    """Start localhost HTTP, TLS, and TCP fixtures and describe them as targets."""
    directory = Path(directory)
    fixtures = DemoFixtures()
    directory.mkdir(parents=True, exist_ok=True)

    marketing = _http(200, b"Acme status: operational\n", jitter=True)
    billing = _http(500, b"billing unavailable\n")
    checkout = _http(200, b"checkout is in maintenance\n")
    api = _http(200, b"{\"ok\":true}\n", jitter=True)
    cdn = _http(200, b"asset\n", delay=0.25)
    docs = _https(directory, "docs.local", 200, b"documentation\n", days=200)
    legacy = _https(directory, "legacy.local", 200, b"admin\n", days=5)
    postgres_sock, postgres_port = _tcp_sink()
    redis_port = _closed_port()
    search_port = _closed_port()

    fixtures._servers.extend([marketing, billing, checkout, api, cdn, docs, legacy])
    fixtures._sockets.append(postgres_sock)

    def http_target(name, group, server, **extra) -> TargetConfig:
        port = server.server_address[1]
        return TargetConfig(
            url=f"http://127.0.0.1:{port}/",
            name=name,
            group=group,
            check_type="http",
            timeout=2,
            retries=0,
            alert_threshold=1,
            **extra,
        )

    def https_target(name, group, server, **extra) -> TargetConfig:
        port = server.server_address[1]
        return TargetConfig(
            url=f"https://127.0.0.1:{port}/",
            name=name,
            group=group,
            check_type="http",
            timeout=2,
            retries=0,
            verify_ssl=False,
            alert_threshold=1,
            ssl_warn_days=14,
            **extra,
        )

    fixtures.targets = [
        http_target("Marketing site", "Edge", marketing, contains=["Acme"]),
        https_target("Docs", "Edge", docs),
        https_target("Legacy admin", "Edge", legacy),
        http_target("API gateway", "Product", api),
        http_target("Billing", "Product", billing),
        http_target("Checkout", "Product", checkout, contains=["ready"]),
        http_target("Image CDN", "Product", cdn, max_latency_ms=120),
        TargetConfig(
            url=f"tcp://127.0.0.1:{postgres_port}",
            name="Postgres",
            group="Data",
            check_type="tcp",
            host="127.0.0.1",
            port=postgres_port,
            timeout=2,
            retries=0,
        ),
        TargetConfig(
            url=f"tcp://127.0.0.1:{redis_port}",
            name="Redis",
            group="Data",
            check_type="tcp",
            host="127.0.0.1",
            port=redis_port,
            timeout=1,
            retries=0,
        ),
        TargetConfig(
            url=f"http://127.0.0.1:{search_port}/",
            name="Search",
            group="Product",
            check_type="http",
            timeout=1,
            retries=0,
        ),
    ]
    return fixtures


def openssl_available() -> bool:
    return shutil.which("openssl") is not None


def _http(status: int, body: bytes, *, delay: float = 0, jitter: bool = False) -> ThreadingHTTPServer:
    handler = _handler(status, body, delay=delay, jitter=jitter)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, name="demo-http", daemon=True)
    thread.start()
    return server


def _https(directory: Path, common_name: str, status: int, body: bytes, *, days: int) -> ThreadingHTTPServer:
    cert, key = _make_cert(directory, common_name, days)
    handler = _handler(status, body)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, name="demo-tls", daemon=True)
    thread.start()
    return server


def _handler(status: int, body: bytes, *, delay: float = 0, jitter: bool = False):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if jitter:
                time.sleep(random.uniform(0.01, 0.04))
            elif delay:
                time.sleep(delay)
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args) -> None:
            return

    return Handler


def _make_cert(directory: Path, common_name: str, days: int) -> tuple[Path, Path]:
    if not openssl_available():
        raise RuntimeError("openssl is required to mint the demo TLS certificates")
    cert = directory / f"{common_name}.crt"
    key = directory / f"{common_name}.key"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            str(days),
            "-nodes",
            "-subj",
            f"/CN={common_name}",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return cert, key


def _tcp_sink() -> tuple[socket.socket, int]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(16)
    port = sock.getsockname()[1]

    def accept() -> None:
        while True:
            try:
                conn, _addr = sock.accept()
            except OSError:
                return
            conn.close()

    threading.Thread(target=accept, name="demo-tcp", daemon=True).start()
    return sock, port


def _closed_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port
