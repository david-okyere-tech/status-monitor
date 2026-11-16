"""Shared data types for targets, check results, and runtime settings."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

SCHEMA_VERSION = 5

DEFAULT_INTERVAL = 60
DEFAULT_TIMEOUT = 10.0
DEFAULT_RETRIES = 2
DEFAULT_ALERT_THRESHOLD = 1
DEFAULT_LOG_DIR = "./logs"
DEFAULT_COOLDOWN_CHECKS = 3
DEFAULT_WEB_HOST = "127.0.0.1"
DEFAULT_WEB_PORT = 8080
DEFAULT_CONCURRENCY = 50
DEFAULT_RETENTION_DAYS = 30
DEFAULT_USER_AGENT = "StatusMonitor/5.0"

LOCK_FILENAME = "status_monitor.lock"
STATE_FILENAME = ".state.json"
HISTORY_FILENAME = "history.jsonl"
SQLITE_FILENAME = "history.sqlite"
MAX_DOWNTIME_PERIODS = 100
EMAIL_SEND_TIMEOUT = 15
WEBHOOK_TIMEOUT = 10


@dataclass
class Webhook:
    url: str
    kind: str = "generic"  # generic, slack, discord


@dataclass
class TargetConfig:
    url: str
    expected_statuses: list[int] = field(default_factory=lambda: [200])
    timeout: float = DEFAULT_TIMEOUT
    retries: int = DEFAULT_RETRIES
    verify_ssl: bool = True
    alert_threshold: int = DEFAULT_ALERT_THRESHOLD
    name: str = ""
    check_type: str = "http"  # http, tcp, ping
    host: str = ""
    port: int = 0
    group: str = "Monitors"
    contains: list[str] = field(default_factory=list)
    regex: Optional[str] = None
    method: str = "GET"
    headers: dict[str, str] = field(default_factory=dict)
    max_latency_ms: Optional[float] = None
    ssl_warn_days: Optional[int] = None
    enabled: bool = True
    follow_redirects: bool = True
    user_agent: str = DEFAULT_USER_AGENT

    def __post_init__(self) -> None:
        if not self.name:
            self.name = self.url
        if not self.group:
            self.group = "Monitors"


@dataclass
class CheckResult:
    url: str
    timestamp: str
    status: str  # UP, DOWN, WARNING
    status_code: Optional[int]
    response_time_ms: Optional[float]
    error: Optional[str]
    attempt: int
    name: str = ""
    check_type: str = "http"
    ssl_days_remaining: Optional[int] = None
    ts_unix: float = 0.0

    def to_dict(self) -> dict:
        response_ms = None
        if self.response_time_ms is not None:
            response_ms = round(self.response_time_ms, 1)
        return {
            "v": SCHEMA_VERSION,
            "url": self.url,
            "name": self.name,
            "type": self.check_type,
            "timestamp": self.timestamp,
            "status": self.status,
            "status_code": self.status_code,
            "response_time_ms": response_ms,
            "error": self.error,
            "attempt": self.attempt,
            "ssl_days_remaining": self.ssl_days_remaining,
        }

    def line(self) -> str:
        """One console/log line. UP/WARNING/DOWN wording matches the v4 report."""
        if self.status == "UP":
            icon = "✅"
            if self.status_code is not None:
                detail = f"UP ({self.status_code})"
            elif self.response_time_ms is not None:
                detail = f"UP ({self.response_time_ms:.0f}ms)"
            else:
                detail = "UP"
            extra = ""
        elif self.status == "WARNING":
            icon = "⚠️"
            if self.status_code is not None:
                detail = f"WARNING ({self.status_code})"
            else:
                detail = "WARNING"
            extra = f" {self.error}" if self.error else ""
        else:
            icon = "🚨"
            detail = "DOWN"
            extra = f" {self.error}" if self.error else ""
        return f"{icon} [{self.timestamp}] {self.url}: {detail}{extra}"


@dataclass
class Settings:
    targets: list[TargetConfig]
    interval: float = DEFAULT_INTERVAL
    email: Optional[str] = None
    webhooks: list[Webhook] = field(default_factory=list)
    cooldown_checks: int = DEFAULT_COOLDOWN_CHECKS
    log_dir: str = DEFAULT_LOG_DIR
    once: bool = False
    web: bool = False
    web_host: str = DEFAULT_WEB_HOST
    web_port: int = DEFAULT_WEB_PORT
    dashboard: bool = False
    quiet: bool = False
    verbose: bool = False
    concurrency: int = DEFAULT_CONCURRENCY
    retention_days: int = DEFAULT_RETENTION_DAYS
    user_agent: str = DEFAULT_USER_AGENT
