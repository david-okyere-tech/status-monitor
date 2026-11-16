import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from status_monitor.cli import main
from status_monitor.web import Hub, render_page, start_web


class _OkHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = b"healthy-marker" if self.path.startswith("/ok") else b"nope"
        code = 200 if self.path.startswith("/ok") else 500
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        return


@pytest.fixture
def http_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _OkHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def _page_payload():
    return {
        "version": "5.0.0",
        "generated_at": "2026-11-16 12:00:00",
        "generated_unix": 1,
        "interval": 30,
        "cycle": 4,
        "overall": "outage",
        "headline": "Partial outage",
        "summary": {
            "monitors": 1,
            "up": 0,
            "warning": 0,
            "down": 1,
            "pending": 0,
            "uptime_24h": 50,
            "open_incidents": 1,
            "latency_p50": 12,
        },
        "groups": [
            {
                "name": "Edge",
                "targets": [
                    {
                        "name": "<script>alert(1)</script>",
                        "url": "http://evil.test/?q=<b>",
                        "type": "http",
                        "status": "DOWN",
                        "status_code": None,
                        "response_time_ms": 12,
                        "error": "Connection refused",
                        "ssl_days_remaining": 3,
                        "uptime_pct": 50,
                        "recent": [
                            {"status": "UP", "ms": 10},
                            {"status": "DOWN", "ms": None},
                        ],
                    }
                ],
            }
        ],
        "incidents": [
            {"name": "Search", "url": "http://search", "started_at": "t0", "ended_at": None, "error": "refused"}
        ],
    }


def test_page_escapes_untrusted_text_and_renders_status():
    html = render_page(_page_payload())
    assert "Partial outage" in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<script>alert" not in html
    assert "Connection refused" in html
    assert 'class="cell up"' in html
    assert 'class="cell down"' in html


def test_web_server_serves_api_and_health():
    import urllib.request

    hub = Hub(30)
    hub.seed(_page_payload())
    server = start_web(hub, "127.0.0.1", 0)
    try:
        port = server.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status") as response:
            payload = json.load(response)
        assert payload["headline"] == "Partial outage"
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/") as response:
            page = response.read().decode()
        assert "Partial outage" in page
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz") as response:
            assert json.load(response)["ready"] is False
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/favicon.svg") as response:
            assert response.status == 200
    finally:
        server.shutdown()
        server.server_close()


def test_cli_once_writes_history_and_summary(tmp_path, http_server, capsys):
    port = http_server.server_address[1]
    with pytest.raises(SystemExit) as exc:
        main(
            [
                "--url",
                f"http://127.0.0.1:{port}/ok",
                "--contains",
                "healthy-marker",
                "--once",
                "--retries",
                "0",
                "--timeout",
                "2",
                "--interval",
                "1",
                "--log-dir",
                str(tmp_path),
            ]
        )
    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert f"🔍 Monitoring 1 URL(s) every 1s (Async)" in output
    assert "UP (200)" in output
    assert "Uptime: 100.0%" in output
    assert "MONITORING SUMMARY" in output
    record = json.loads((tmp_path / "history.jsonl").read_text().splitlines()[0])
    assert record["status"] == "UP"
    assert record["status_code"] == 200
    assert (tmp_path / "history.sqlite").exists()
    assert (tmp_path / "monitor.log").exists()

    main(["report", "--log-dir", str(tmp_path), "--hours", "0"])
    report = capsys.readouterr().out
    assert "uptime" in report
    assert "100.00%" in report


def test_cli_validation_and_validate_command(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--url", "https://example.com", "--retries", "-1"])
    assert exc.value.code == 2
    assert "retries" in capsys.readouterr().err

    with pytest.raises(SystemExit) as missing:
        main([])
    assert missing.value.code == 2

    path = tmp_path / "config.json"
    path.write_text(json.dumps({"targets": [{"url": "https://example.com", "name": "Example"}]}))
    main(["validate", "--config", str(path)])
    assert "OK: 1 target" in capsys.readouterr().out

    with pytest.raises(SystemExit) as help_exit:
        main(["--help"])
    assert help_exit.value.code == 0


def test_down_target_alerts_once_in_state(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        main(
            [
                "--url",
                "http://127.0.0.1:1/",
                "--once",
                "--retries",
                "0",
                "--timeout",
                "1",
                "--interval",
                "1",
                "--email",
                "ops@example.com",
                "--log-dir",
                str(tmp_path),
            ]
        )
    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "DOWN" in output
    assert "Email alerts skipped" in output
    state = json.loads((tmp_path / ".state.json").read_text())
    assert state["http://127.0.0.1:1/"]["alert_fired"] is True
