import asyncio
import subprocess
from datetime import datetime, timezone

import httpx
import pytest

from status_monitor.checks import (
    apply_success_checks,
    check_http,
    check_ping,
    check_target,
    check_tcp,
    classify_ping,
    connect_error_message,
    days_until,
    extract_validity,
    make_result,
)
from status_monitor.models import CheckResult, TargetConfig


def _target(url="http://example.com/health", **kwargs):
    values = dict(url=url, timeout=2, retries=0, name="Example")
    values.update(kwargs)
    return TargetConfig(**values)


def test_result_line_matches_the_console_contract():
    up = make_result(_target(), "UP", 200, 142.3, None, 1)
    assert f"{up.url}: UP (200)" in up.line()
    warning = make_result(_target(), "WARNING", 503, 10, "Expected [200], got 503", 3)
    assert "WARNING (503) Expected [200], got 503" in warning.line()
    down = make_result(_target(), "DOWN", None, None, "Connection failed", 3)
    assert down.line().endswith("DOWN Connection failed")
    assert "(None)" not in down.line()
    record = up.to_dict()
    assert record["v"] == 5
    assert record["status"] == "UP"
    assert record["response_time_ms"] == 142.3


def test_connect_error_keeps_the_plain_message():
    assert connect_error_message(RuntimeError("refused")) == "Connection failed"
    assert connect_error_message(RuntimeError("certificate verify failed")).startswith("TLS error:")


def test_classify_ping():
    sample = "64 bytes from 1.1.1.1: icmp_seq=1 ttl=57 time=12.3 ms\n1 packets transmitted, 1 received, 0% packet loss"
    status, elapsed, error = classify_ping(sample, 0)
    assert (status, elapsed, error) == ("UP", 12.3, None)
    loss = "1 packets transmitted, 0 received, 100% packet loss"
    assert classify_ping(loss, 1)[0] == "DOWN"
    assert "packet loss" in classify_ping(loss, 1)[2]


def test_content_and_latency_only_downgrade_up():
    target = _target(contains=["ready"], max_latency_ms=50)
    result = CheckResult("http://example.com/health", "t", "UP", 200, 80, None, 1)
    apply_success_checks(result, target, "not the word")
    assert result.status == "WARNING"
    assert result.error.startswith("Missing content:")

    slow = CheckResult("http://example.com/health", "t", "UP", 200, 80, None, 1)
    apply_success_checks(slow, _target(max_latency_ms=50), None)
    assert "Slow response" in slow.error

    down = CheckResult("http://example.com/health", "t", "DOWN", None, None, "Connection failed", 1)
    apply_success_checks(down, target, "")
    assert down.status == "DOWN"


def test_http_success_retries_and_timeout(monkeypatch):
    async def no_wait(_attempt):
        return None

    monkeypatch.setattr("status_monitor.checks._sleep_backoff", no_wait)

    calls = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, text="nope")
        return httpx.Response(200, text="ready")

    async def run():
        transport = httpx.MockTransport(flaky)
        async with httpx.AsyncClient(transport=transport) as client:
            return await check_http(client, _target(retries=1, contains=["ready"]))

    recovered = asyncio.run(run())
    assert recovered.status == "UP"
    assert recovered.attempt == 2
    assert calls["n"] == 2

    def fail(_request):
        raise httpx.ConnectTimeout("timed out")

    async def down():
        async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
            return await check_http(client, _target(retries=2))

    result = asyncio.run(down())
    assert result.status == "DOWN"
    assert result.error == "Request timed out"
    assert result.attempt == 3


def test_user_agent_and_unexpected_status():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["ua"] = request.headers["user-agent"]
        seen["x-token"] = request.headers["x-token"]
        return httpx.Response(404, text="missing")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await check_http(
                client,
                _target(user_agent="StatusMonitor/test", headers={"X-Token": "abc"}, retries=0),
            )

    result = asyncio.run(run())
    assert result.status == "WARNING"
    assert result.status_code == 404
    assert seen["ua"] == "StatusMonitor/test"
    assert seen["x-token"] == "abc"


def test_tcp_up_and_refused():
    async def scenario():
        server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            up = await check_tcp(_target(url=f"tcp://127.0.0.1:{port}", check_type="tcp", host="127.0.0.1", port=port))
        finally:
            server.close()
            await server.wait_closed()
        import socket

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        closed_port = sock.getsockname()[1]
        sock.close()
        closed = await check_tcp(
            _target(
                url=f"tcp://127.0.0.1:{closed_port}",
                check_type="tcp",
                host="127.0.0.1",
                port=closed_port,
                timeout=1,
            )
        )
        return up, closed

    up, closed = asyncio.run(scenario())
    assert up.status == "UP"
    assert up.response_time_ms is not None
    assert closed.status == "DOWN"
    assert closed.error == "Connection refused"


def test_days_until_and_certificate_parser(tmp_path):
    assert days_until(datetime(2026, 1, 10), datetime(2026, 1, 1)) == 9
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-keyout", str(key),
            "-out", str(cert), "-days", "30", "-nodes", "-subj", "/CN=localhost",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    der = subprocess.check_output(["openssl", "x509", "-in", str(cert), "-outform", "DER"])
    _not_before, not_after = extract_validity(der)
    end = subprocess.check_output(["openssl", "x509", "-in", str(cert), "-noout", "-enddate"], text=True)
    stamp = end.split("=", 1)[1].strip().removesuffix(" GMT")
    expected = datetime.strptime(stamp, "%b %d %H:%M:%S %Y")
    assert abs((expected - not_after).total_seconds()) < 2
    assert days_until(not_after, datetime.now(timezone.utc).replace(tzinfo=None)) >= 28


def test_ping_missing_binary(monkeypatch):
    async def boom(*_args, **_kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(asyncio, "create_subprocess_exec", boom)
    result = asyncio.run(check_ping(_target(url="ping://127.0.0.1", check_type="ping", host="127.0.0.1")))
    assert result.status == "DOWN"
    assert "not available" in result.error


def test_check_target_swallows_unexpected_errors(monkeypatch):
    async def explode(*_args, **_kwargs):
        raise RuntimeError("nope")

    monkeypatch.setattr("status_monitor.checks.check_tcp", explode)
    result = asyncio.run(check_target(_target(url="tcp://127.0.0.1:9", check_type="tcp", host="127.0.0.1", port=9)))
    assert result.status == "DOWN"
    assert "Check error" in result.error
