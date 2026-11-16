"""Build the status document shared by the web page, API, and terminal UI."""

from __future__ import annotations

import time
from typing import Optional

from status_monitor import __version__
from status_monitor.history import HistoryStore
from status_monitor.models import CheckResult, TargetConfig
from status_monitor.stats import percentile, uptime_percent

_HEADLINES = {
    "operational": "All systems operational",
    "degraded": "Degraded performance",
    "outage": "Partial outage",
    "major": "Major outage",
    "pending": "Waiting for the first check",
}


def overall_state(statuses: list[str]) -> str:
    if not statuses or all(status == "PENDING" for status in statuses):
        return "pending"
    live = [status for status in statuses if status != "PENDING"]
    if any(status == "DOWN" for status in live):
        if all(status == "DOWN" for status in live):
            return "major"
        return "outage"
    if any(status == "WARNING" for status in live):
        return "degraded"
    return "operational"


def build_payload(
    targets: list[TargetConfig],
    history: HistoryStore,
    latest: dict[str, CheckResult],
    *,
    interval: float,
    cycle: int,
    now: Optional[float] = None,
) -> dict:
    now = time.time() if now is None else now
    since = now - 86400
    groups: dict[str, list] = {}
    group_order: list[str] = []
    statuses: list[str] = []

    for target in targets:
        group = target.group or "Monitors"
        if group not in groups:
            groups[group] = []
            group_order.append(group)
        result = latest.get(target.url)
        stored = None
        if result is None:
            latest_fn = getattr(history, "latest", None)
            if callable(latest_fn):
                stored = latest_fn(target.url)
        counts = history.counts(target.url, since)
        all_counts = history.counts(target.url, None)
        samples = history.latencies(target.url, since, limit=2000)
        recent = history.recent(target.url, 36)
        row = _target_row(target, result, stored, counts, all_counts, samples, recent)
        statuses.append(row["status"])
        groups[group].append(row)

    global_counts = history.counts(None, since)
    global_samples = history.latencies(None, since, limit=2000)
    up = sum(1 for status in statuses if status == "UP")
    warning = sum(1 for status in statuses if status == "WARNING")
    down = sum(1 for status in statuses if status == "DOWN")
    pending = sum(1 for status in statuses if status == "PENDING")
    state = overall_state(statuses)
    return {
        "version": __version__,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
        "generated_unix": now,
        "interval": interval,
        "cycle": cycle,
        "overall": state,
        "headline": _HEADLINES[state],
        "summary": {
            "monitors": len(targets),
            "up": up,
            "warning": warning,
            "down": down,
            "pending": pending,
            "uptime_24h": uptime_percent(
                global_counts.get("UP", 0),
                global_counts.get("WARNING", 0),
                global_counts.get("DOWN", 0),
            ),
            "open_incidents": history.open_incident_count(),
            "latency_p50": percentile(global_samples, 50),
        },
        "groups": [{"name": name, "targets": groups[name]} for name in group_order],
        "incidents": history.incidents(8),
    }


def empty_payload(targets: list[TargetConfig], *, interval: float) -> dict:
    return build_payload(targets, _EmptyHistory(), {}, interval=interval, cycle=0)


class _EmptyHistory:
    """Stand-in so the page can render before the database is opened."""

    def counts(self, url, since):
        return {}

    def latencies(self, url, since, limit=2000):
        return []

    def recent(self, url, limit=40):
        return []

    def open_incident_count(self):
        return 0

    def incidents(self, limit=8):
        return []


def _target_row(target, result, stored, counts, all_counts, samples, recent) -> dict:
    if result is not None:
        status = result.status
        status_code = result.status_code
        response_time_ms = result.response_time_ms
        error = result.error
        ssl_days = result.ssl_days_remaining
        checked_at = result.timestamp
    elif stored:
        status = stored["status"]
        status_code = stored["status_code"]
        response_time_ms = stored["response_time_ms"]
        error = stored["error"]
        ssl_days = stored["ssl_days_remaining"]
        checked_at = stored["ts"]
    else:
        status = "PENDING"
        status_code = None
        response_time_ms = None
        error = None
        ssl_days = None
        checked_at = None
    return {
        "name": target.name,
        "url": target.url,
        "type": target.check_type,
        "status": status,
        "status_code": status_code,
        "response_time_ms": response_time_ms,
        "error": error,
        "ssl_days_remaining": ssl_days,
        "checked_at": checked_at,
        "uptime_pct": uptime_percent(counts.get("UP", 0), counts.get("WARNING", 0), counts.get("DOWN", 0)),
        "uptime_all": uptime_percent(
            all_counts.get("UP", 0), all_counts.get("WARNING", 0), all_counts.get("DOWN", 0)
        ),
        "p50": percentile(samples, 50),
        "p95": percentile(samples, 95),
        "p99": percentile(samples, 99),
        "recent": recent,
    }
