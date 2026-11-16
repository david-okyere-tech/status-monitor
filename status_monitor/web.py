"""HTTP status page and JSON API, served from the monitor process."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import urlsplit

from status_monitor.stats import fmt_ms, fmt_pct

_FAVICON = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">
<rect width="32" height="32" rx="8" fill="#10140f"/>
<circle cx="16" cy="16" r="6" fill="#c6f135"/>
</svg>
""".encode()

_CSS = """
:root {
  --bg: #10140f;
  --raise: #171d16;
  --raise-2: #1e261c;
  --ink: #f4f1e6;
  --muted: #a3aa98;
  --faint: #6e7664;
  --line: rgba(244, 241, 230, 0.1);
  --up: #c6f135;
  --warn: #ffc14a;
  --down: #ff6a45;
  --shadow: 0 24px 60px rgba(0, 0, 0, 0.28);
  --sans: "Outfit", "Avenir Next", "Segoe UI", sans-serif;
  --serif: "Fraunces", "Iowan Old Style", Palatino, Georgia, serif;
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
  min-height: 100vh;
  color: var(--ink);
  font-family: var(--sans);
  background:
    radial-gradient(900px 420px at 10% -10%, rgba(198, 241, 53, 0.14), transparent 55%),
    radial-gradient(700px 380px at 100% 0%, rgba(255, 106, 69, 0.08), transparent 50%),
    var(--bg);
}
body.state-operational { --accent: var(--up); }
body.state-degraded { --accent: var(--warn); }
body.state-outage, body.state-major { --accent: var(--down); }
body.state-pending { --accent: var(--muted); }
.bar {
  height: 4px;
  background: var(--accent);
}
.wrap {
  width: min(1080px, calc(100% - 32px));
  margin: 0 auto;
  padding: 28px 0 72px;
}
header.top {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  align-items: baseline;
}
.brand {
  display: flex;
  align-items: center;
  gap: 10px;
  letter-spacing: 0.16em;
  text-transform: uppercase;
  font-size: 12px;
  color: var(--muted);
}
.mark {
  width: 10px;
  height: 10px;
  border-radius: 2px;
  background: var(--accent);
  box-shadow: 0 0 0 4px rgba(198, 241, 53, 0.08);
}
.clock {
  color: var(--faint);
  font-size: 12px;
  letter-spacing: 0.04em;
}
.hero { margin: 28px 0 22px; }
.kicker {
  margin: 0 0 8px;
  color: var(--accent);
  letter-spacing: 0.18em;
  text-transform: uppercase;
  font-size: 12px;
}
h1 {
  margin: 0;
  font-family: var(--serif);
  font-weight: 560;
  font-size: clamp(42px, 6vw, 72px);
  line-height: 0.95;
  letter-spacing: -0.035em;
}
.sub {
  margin: 12px 0 0;
  color: var(--muted);
  font-size: 15px;
}
.stats {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 12px;
  margin: 26px 0 32px;
}
.stat {
  background: var(--raise);
  border: 1px solid var(--line);
  border-radius: 16px;
  padding: 16px 18px 14px;
  box-shadow: var(--shadow);
}
.stat span {
  display: block;
  color: var(--faint);
  font-size: 12px;
  letter-spacing: 0.12em;
  text-transform: uppercase;
}
.stat strong {
  display: block;
  margin-top: 6px;
  font-family: var(--serif);
  font-size: 34px;
  font-weight: 560;
  letter-spacing: -0.03em;
}
.group { margin: 22px 0 8px; }
.group h2 {
  margin: 0 0 10px;
  font-size: 12px;
  letter-spacing: 0.16em;
  text-transform: uppercase;
  color: var(--faint);
  font-weight: 500;
}
.target {
  display: grid;
  grid-template-columns: minmax(200px, 1.15fr) minmax(180px, 1fr) minmax(150px, auto);
  gap: 16px;
  align-items: center;
  padding: 14px 16px;
  margin-bottom: 8px;
  background: rgba(23, 29, 22, 0.92);
  border: 1px solid var(--line);
  border-radius: 14px;
}
.target.down { box-shadow: inset 3px 0 0 var(--down); }
.target.warning { box-shadow: inset 3px 0 0 var(--warn); }
.target.up { box-shadow: inset 3px 0 0 transparent; }
.identity { min-width: 0; }
.name-row { display: flex; align-items: center; gap: 8px; }
.name { font-weight: 640; font-size: 16px; }
.kind {
  color: var(--faint);
  font-size: 10px;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  border: 1px solid var(--line);
  border-radius: 999px;
  padding: 2px 6px;
}
.url {
  margin-top: 3px;
  color: var(--faint);
  font-size: 12px;
  overflow-wrap: anywhere;
}
.cells { display: flex; gap: 3px; align-items: flex-end; height: 28px; }
.cell {
  width: 8px;
  height: 18px;
  border-radius: 2px;
  background: #2a332c;
}
.cell.up { background: var(--up); }
.cell.warning { background: var(--warn); }
.cell.down { background: var(--down); }
.cell.empty { background: transparent; box-shadow: inset 0 0 0 1px rgba(244, 241, 230, 0.12); }
.metrics {
  display: flex;
  justify-content: flex-end;
  align-items: center;
  gap: 14px;
  text-align: right;
}
.metric span {
  display: block;
  color: var(--faint);
  font-size: 10px;
  letter-spacing: 0.12em;
  text-transform: uppercase;
}
.metric strong { font-size: 15px; font-weight: 640; }
.pill {
  font-size: 11px;
  letter-spacing: 0.08em;
  padding: 4px 8px;
  border-radius: 999px;
  text-transform: uppercase;
}
.pill.up { background: rgba(198, 241, 53, 0.14); color: var(--up); }
.pill.warning { background: rgba(255, 193, 74, 0.14); color: var(--warn); }
.pill.down { background: rgba(255, 106, 69, 0.16); color: var(--down); }
.pill.pending { background: rgba(163, 170, 152, 0.12); color: var(--muted); }
.detail {
  grid-column: 1 / -1;
  margin: -6px 0 0;
  color: var(--muted);
  font-size: 13px;
}
.spark { display: block; margin-top: 6px; }
.incidents { margin-top: 28px; }
.incidents h2 {
  font-family: var(--serif);
  font-size: 28px;
  font-weight: 560;
  margin: 0 0 12px;
}
.incident {
  display: grid;
  grid-template-columns: 92px 1fr;
  gap: 12px;
  padding: 12px 0;
  border-top: 1px solid var(--line);
}
.badge {
  font-size: 11px;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--down);
}
.badge.resolved { color: var(--up); }
.incident strong { font-weight: 640; }
.incident p { margin: 3px 0 0; color: var(--muted); font-size: 13px; }
footer {
  margin-top: 36px;
  color: var(--faint);
  font-size: 12px;
  display: flex;
  justify-content: space-between;
  gap: 12px;
}
footer a { color: var(--muted); }
@media (max-width: 800px) {
  .stats { grid-template-columns: 1fr; }
  .target { grid-template-columns: 1fr; }
  .metrics { justify-content: flex-start; }
  header.top { flex-direction: column; }
}
"""


class Hub:
    """Latest status document. The monitor publishes; the web thread reads."""

    def __init__(self, interval: float):
        self.interval = interval
        self._lock = threading.Lock()
        self._payload: dict = {
            "version": "",
            "generated_at": "",
            "generated_unix": time.time(),
            "interval": interval,
            "cycle": 0,
            "overall": "pending",
            "headline": "Waiting for the first check",
            "summary": {
                "monitors": 0,
                "up": 0,
                "warning": 0,
                "down": 0,
                "pending": 0,
                "uptime_24h": None,
                "open_incidents": 0,
                "latency_p50": None,
            },
            "groups": [],
            "incidents": [],
        }
        self.published_unix: Optional[float] = None
        self.cycles_completed = 0

    def seed(self, payload: dict) -> None:
        with self._lock:
            self._payload = payload

    def publish(self, payload: dict) -> None:
        with self._lock:
            self._payload = payload
            self.published_unix = time.time()
            self.cycles_completed += 1

    def payload(self) -> dict:
        with self._lock:
            return self._payload

    def health(self, now: Optional[float] = None) -> tuple[int, dict]:
        now = time.time() if now is None else now
        with self._lock:
            completed = self.cycles_completed
            published = self.published_unix
            interval = self.interval
        if completed == 0 or published is None:
            return 200, {"status": "starting", "ready": False}
        age = now - published
        limit = max(interval * 3, interval + 30)
        body = {"status": "ok", "ready": True, "age_seconds": round(age, 1), "cycles": completed}
        if age > limit:
            body["status"] = "stale"
            body["ready"] = False
            return 503, body
        return 200, body


def render_page(payload: dict) -> str:
    overall = payload.get("overall", "pending")
    if overall not in {"operational", "degraded", "outage", "major", "pending"}:
        overall = "pending"
    summary = payload.get("summary") or {}
    body = _render_body(payload, summary)
    generated = payload.get("generated_unix") or 0
    title = _esc(payload.get("headline") or "Status")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="8">
<title>{title} · Status Monitor</title>
<link rel="icon" href="/favicon.svg">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,560;9..144,640&family=Outfit:wght@400;560;640&display=swap" rel="stylesheet">
<style>{_CSS}</style>
</head>
<body class="state-{overall}" data-generated="{generated}">
<div class="bar"></div>
{body}
<script>
const started = Number(document.body.dataset.generated || 0);
function tick() {{
  const el = document.getElementById("age");
  if (!el || !started) return;
  const seconds = Math.max(0, Math.round(Date.now() / 1000 - started));
  el.textContent = seconds < 5 ? "just now" : seconds < 60 ? seconds + "s ago" : Math.floor(seconds / 60) + "m ago";
}}
tick();
setInterval(tick, 1000);
</script>
</body>
</html>
"""


def _render_body(payload: dict, summary: dict) -> str:
    uptime = fmt_pct(summary.get("uptime_24h"), digits=2)
    median = fmt_ms(summary.get("latency_p50"))
    incidents_open = summary.get("open_incidents") or 0
    parts = [
        '<div class="wrap">',
        '<header class="top">',
        '<div class="brand"><span class="mark"></span>Status Monitor</div>',
        (
            f'<div class="clock">v{_esc(payload.get("version"))}'
            f' · every {_esc(_interval_label(payload.get("interval")))}'
            f' · cycle {_esc(payload.get("cycle"))}</div>'
        ),
        "</header>",
        '<section class="hero">',
        '<p class="kicker">Live status</p>',
        f'<h1 role="status">{_esc(payload.get("headline"))}</h1>',
        (
            f'<p class="sub">{_esc(summary.get("up", 0))} up'
            f' · {_esc(summary.get("warning", 0))} degraded'
            f' · {_esc(summary.get("down", 0))} down'
            f' · checked <span id="age">{_esc(payload.get("generated_at") or "—")}</span></p>'
        ),
        "</section>",
        '<section class="stats">',
        f'<div class="stat"><span>24h uptime</span><strong>{_esc(uptime)}</strong></div>',
        f'<div class="stat"><span>Open incidents</span><strong>{_esc(incidents_open)}</strong></div>',
        f'<div class="stat"><span>Median latency</span><strong>{_esc(median)}</strong></div>',
        "</section>",
    ]
    for group in payload.get("groups") or []:
        parts.append(f'<section class="group"><h2>{_esc(group.get("name"))}</h2>')
        for target in group.get("targets") or []:
            parts.append(_render_target(target))
        parts.append("</section>")
    incidents = payload.get("incidents") or []
    if incidents:
        parts.append('<section class="incidents"><h2>Recent incidents</h2>')
        for incident in incidents:
            resolved = bool(incident.get("ended_at"))
            badge = "resolved" if resolved else "ongoing"
            when = (
                f"{incident.get('started_at')} → {incident.get('ended_at')}"
                if resolved
                else f"since {incident.get('started_at')}"
            )
            error = incident.get("error") or ""
            parts.append(
                '<article class="incident">'
                f'<div class="badge {"resolved" if resolved else ""}">{badge}</div>'
                f"<div><strong>{_esc(incident.get('name'))}</strong>"
                f"<p>{_esc(when)}"
                + (f" · {_esc(error)}" if error else "")
                + "</p></div></article>"
            )
        parts.append("</section>")
    parts.append(
        '<footer><span>Reachability counts warnings as up. Latency is the last 24 hours.</span>'
        '<a href="/api/status">JSON</a></footer></div>'
    )
    return "".join(parts)


def _render_target(target: dict) -> str:
    status = str(target.get("status") or "PENDING").upper()
    css = status.lower() if status.lower() in {"up", "warning", "down", "pending"} else "pending"
    stroke = {"up": "#c6f135", "warning": "#ffc14a", "down": "#ff6a45"}.get(css, "#a3aa98")
    samples = [item.get("ms") for item in target.get("recent") or [] if item.get("ms") is not None]
    spark = _sparkline_svg(samples, stroke)
    tls = "—"
    if target.get("ssl_days_remaining") is not None:
        tls = f"{target['ssl_days_remaining']}d"
    error = target.get("error")
    detail = f'<p class="detail">{_esc(error)}</p>' if error else ""
    code = target.get("status_code")
    latency = fmt_ms(target.get("response_time_ms"))
    if code is not None and target.get("response_time_ms") is not None:
        latency = f"{latency}"
    return (
        f'<article class="target {css}">'
        '<div class="identity">'
        '<div class="name-row">'
        f'<div class="name">{_esc(target.get("name"))}</div>'
        f'<span class="kind">{_esc(target.get("type"))}</span>'
        f'<span class="pill {css}">{_esc(status)}</span>'
        "</div>"
        f'<div class="url">{_esc(target.get("url"))}</div>'
        f"{spark}"
        "</div>"
        f'<div>{_cells(target.get("recent") or [])}</div>'
        '<div class="metrics">'
        f'<div class="metric"><span>Latency</span><strong>{_esc(latency)}</strong></div>'
        f'<div class="metric"><span>24h</span><strong>{_esc(fmt_pct(target.get("uptime_pct"), digits=2))}</strong></div>'
        f'<div class="metric"><span>TLS</span><strong>{_esc(tls)}</strong></div>'
        "</div>"
        f"{detail}</article>"
    )


def _cells(recent: list[dict]) -> str:
    width = 36
    items = list(recent)[-width:]
    parts = ['<div class="cells" aria-hidden="true">']
    for _ in range(width - len(items)):
        parts.append('<i class="cell empty"></i>')
    for item in items:
        status = str(item.get("status", "")).lower()
        if status not in {"up", "warning", "down"}:
            status = "empty"
        parts.append(f'<i class="cell {status}"></i>')
    parts.append("</div>")
    return "".join(parts)


def _sparkline_svg(values: list[float], stroke: str) -> str:
    if len(values) < 2:
        return ""
    width, height = 132, 28
    sample = values[-30:]
    lo = min(sample)
    hi = max(sample)
    span = hi - lo or 1.0
    points = []
    last = len(sample) - 1
    for index, value in enumerate(sample):
        x = 0 if last == 0 else index * (width / last)
        y = height - 3 - ((value - lo) / span) * (height - 6)
        points.append(f"{x:.1f},{y:.1f}")
    return (
        f'<svg class="spark" viewBox="0 0 {width} {height}" width="{width}" height="{height}" aria-hidden="true">'
        f'<polyline fill="none" stroke="{stroke}" stroke-width="1.7" stroke-linejoin="round" '
        f'stroke-linecap="round" points="{" ".join(points)}"/></svg>'
    )


def _interval_label(interval) -> str:
    try:
        value = float(interval)
    except (TypeError, ValueError):
        return "—"
    if value < 60:
        return f"{value:.0f}s" if value == int(value) else f"{value:g}s"
    return f"{value / 60:.0f}m" if value % 60 == 0 else f"{value:g}s"


def _esc(value) -> str:
    text = "" if value is None else str(value)
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#x27;")
    )


class _Handler(BaseHTTPRequestHandler):
    hub: Hub

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/api/status":
            self._send(200, json.dumps(self.hub.payload()).encode(), "application/json; charset=utf-8")
            return
        if path == "/healthz":
            code, body = self.hub.health()
            self._send(code, json.dumps(body).encode(), "application/json; charset=utf-8")
            return
        if path == "/favicon.svg":
            self._send(200, _FAVICON, "image/svg+xml")
            return
        if path in {"/", "/index.html"}:
            page = render_page(self.hub.payload()).encode()
            self._send(200, page, "text/html; charset=utf-8")
            return
        self._send(404, b'{"error":"not found"}', "application/json; charset=utf-8")

    def log_message(self, fmt: str, *args) -> None:
        return

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)


def start_web(hub: Hub, host: str, port: int) -> ThreadingHTTPServer:
    _Handler.hub = hub
    server = ThreadingHTTPServer((host, port), _Handler)
    thread = threading.Thread(target=server.serve_forever, name="status-web", daemon=True)
    thread.start()
    return server
