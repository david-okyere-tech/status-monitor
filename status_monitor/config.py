"""Load and validate JSON, YAML, and TOML configs.

CLI flags and config files share `parse_target`, so a bad value is rejected
the same way no matter where it came from. Unknown keys are errors — a typo
should not silently disable a check.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

from status_monitor import __version__
from status_monitor.models import (
    DEFAULT_ALERT_THRESHOLD,
    DEFAULT_COOLDOWN_CHECKS,
    DEFAULT_CONCURRENCY,
    DEFAULT_INTERVAL,
    DEFAULT_RETENTION_DAYS,
    DEFAULT_RETRIES,
    DEFAULT_TIMEOUT,
    DEFAULT_USER_AGENT,
    TargetConfig,
    Webhook,
)

USER_AGENT = f"StatusMonitor/{__version__}"

_ALLOWED_ROOT = {"global", "targets", "alerts"}
_ALLOWED_GLOBAL = {
    "expected_statuses",
    "expected_status",
    "timeout",
    "retries",
    "verify_ssl",
    "alert_threshold",
    "interval",
    "ssl_warn_days",
    "max_latency_ms",
    "history_retention_days",
    "cooldown_checks",
    "concurrency",
    "user_agent",
    "follow_redirects",
    "log_dir",
}
_ALLOWED_TARGET = {
    "name",
    "url",
    "type",
    "host",
    "port",
    "group",
    "expected_statuses",
    "expected_status",
    "timeout",
    "retries",
    "verify_ssl",
    "alert_threshold",
    "contains",
    "regex",
    "method",
    "headers",
    "max_latency_ms",
    "ssl_warn_days",
    "enabled",
    "follow_redirects",
}
_ALLOWED_ALERTS = {"email", "webhooks", "cooldown_checks"}
_ALLOWED_WEBHOOK = {"url", "type"}
_HOST_UNSAFE = re.compile(r"[^A-Za-z0-9.:_\-\[\]]")


class ConfigError(ValueError):
    """A config file or CLI target failed validation."""


@dataclass
class AppConfig:
    targets: list[TargetConfig] = field(default_factory=list)
    interval: float = DEFAULT_INTERVAL
    email: Optional[str] = None
    webhooks: list[Webhook] = field(default_factory=list)
    cooldown_checks: int = DEFAULT_COOLDOWN_CHECKS
    log_dir: Optional[str] = None
    concurrency: int = DEFAULT_CONCURRENCY
    history_retention_days: int = DEFAULT_RETENTION_DAYS
    user_agent: str = USER_AGENT


def load_config(path: str) -> AppConfig:
    raw = _read_file(path)
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("config root must be a mapping of global / targets / alerts")
    extra = set(raw) - _ALLOWED_ROOT
    if extra:
        raise ConfigError(f"unknown config key(s): {', '.join(sorted(extra))}")

    global_conf = raw.get("global") or {}
    if not isinstance(global_conf, dict):
        raise ConfigError("global must be a mapping")
    extra = set(global_conf) - _ALLOWED_GLOBAL
    if extra:
        raise ConfigError(f"unknown global key(s): {', '.join(sorted(extra))}")

    alerts_conf = raw.get("alerts") or {}
    if not isinstance(alerts_conf, dict):
        raise ConfigError("alerts must be a mapping")
    extra = set(alerts_conf) - _ALLOWED_ALERTS
    if extra:
        raise ConfigError(f"unknown alerts key(s): {', '.join(sorted(extra))}")

    targets_raw = raw.get("targets") or []
    if not isinstance(targets_raw, list):
        raise ConfigError("targets must be a list")

    targets: list[TargetConfig] = []
    seen: set[str] = set()
    for index, item in enumerate(targets_raw):
        target = parse_target(item, global_conf, where=f"targets[{index}]")
        if target.url in seen:
            raise ConfigError(f"duplicate target url: {target.url}")
        seen.add(target.url)
        targets.append(target)

    interval = _as_float(
        global_conf.get("interval", DEFAULT_INTERVAL),
        "global.interval",
        minimum=0,
        exclusive=True,
    )
    retention = _as_int(
        global_conf.get("history_retention_days", DEFAULT_RETENTION_DAYS),
        "global.history_retention_days",
        minimum=0,
    )
    concurrency = _as_int(
        global_conf.get("concurrency", DEFAULT_CONCURRENCY),
        "global.concurrency",
        minimum=1,
    )
    user_agent = global_conf.get("user_agent", USER_AGENT)
    if not isinstance(user_agent, str) or not user_agent.strip():
        raise ConfigError("global.user_agent must be a non-empty string")

    email = alerts_conf.get("email")
    if email is not None and not isinstance(email, str):
        raise ConfigError("alerts.email must be a string")

    cooldown_source = alerts_conf.get("cooldown_checks", global_conf.get("cooldown_checks", DEFAULT_COOLDOWN_CHECKS))
    cooldown = _as_int(cooldown_source, "cooldown_checks", minimum=0)

    webhooks: list[Webhook] = []
    for index, item in enumerate(alerts_conf.get("webhooks") or []):
        webhooks.append(parse_webhook(item, where=f"alerts.webhooks[{index}]"))

    log_dir = global_conf.get("log_dir")
    if log_dir is not None and (not isinstance(log_dir, str) or not log_dir.strip()):
        raise ConfigError("global.log_dir must be a non-empty string")

    # parse_target reads user_agent from defaults when the global block has it.
    # Targets loaded before that key existed still need the resolved value.
    if "user_agent" not in global_conf:
        for target in targets:
            target.user_agent = user_agent

    return AppConfig(
        targets=targets,
        interval=interval,
        email=email or None,
        webhooks=webhooks,
        cooldown_checks=cooldown,
        log_dir=log_dir,
        concurrency=concurrency,
        history_retention_days=retention,
        user_agent=user_agent,
    )


def parse_target(raw: Any, defaults: dict, *, where: str) -> TargetConfig:
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} must be a mapping")
    extra = set(raw) - _ALLOWED_TARGET
    if extra:
        raise ConfigError(f"{where}: unknown key(s): {', '.join(sorted(extra))}")
    if "expected_statuses" in raw and "expected_status" in raw:
        raise ConfigError(f"{where}: set only one of expected_statuses or expected_status")

    check_type, url, host, port = _identity(raw, where)
    expected = _expected_statuses(raw, defaults, where)
    timeout = _as_float(_pick(raw, defaults, "timeout", DEFAULT_TIMEOUT), f"{where}.timeout", minimum=0, exclusive=True)
    retries = _as_int(_pick(raw, defaults, "retries", DEFAULT_RETRIES), f"{where}.retries", minimum=0)
    verify_ssl = _as_bool(_pick(raw, defaults, "verify_ssl", True), f"{where}.verify_ssl")
    alert_threshold = _as_int(
        _pick(raw, defaults, "alert_threshold", DEFAULT_ALERT_THRESHOLD),
        f"{where}.alert_threshold",
        minimum=1,
    )
    follow = _as_bool(_pick(raw, defaults, "follow_redirects", True), f"{where}.follow_redirects")
    enabled = _as_bool(raw.get("enabled", True), f"{where}.enabled")
    method = str(raw.get("method", "GET")).upper()
    if method not in {"GET", "HEAD"}:
        raise ConfigError(f"{where}.method must be GET or HEAD")

    contains = _contains(raw.get("contains"), where)
    if contains and method == "HEAD":
        raise ConfigError(f"{where}: contains requires method GET")

    regex = raw.get("regex")
    if regex is not None:
        if not isinstance(regex, str) or not regex:
            raise ConfigError(f"{where}.regex must be a non-empty string")
        try:
            re.compile(regex)
        except re.error as exc:
            raise ConfigError(f"{where}.regex is not a valid regular expression: {exc}") from exc
        if method == "HEAD":
            raise ConfigError(f"{where}: regex requires method GET")

    headers = _headers(raw.get("headers"), where)
    max_latency = raw.get("max_latency_ms", defaults.get("max_latency_ms"))
    if max_latency is not None:
        max_latency = _as_float(max_latency, f"{where}.max_latency_ms", minimum=0, exclusive=True)

    ssl_warn = raw.get("ssl_warn_days", defaults.get("ssl_warn_days"))
    ssl_warn_days: Optional[int]
    if ssl_warn is None:
        ssl_warn_days = None
    else:
        ssl_warn_days = _as_int(ssl_warn, f"{where}.ssl_warn_days", minimum=0)
        if ssl_warn_days == 0:
            ssl_warn_days = None

    name = raw.get("name")
    if name is None:
        name = _default_name(check_type, url, host, port)
    elif not isinstance(name, str) or not name.strip():
        raise ConfigError(f"{where}.name must be a non-empty string")

    group = raw.get("group", "Monitors")
    if not isinstance(group, str) or not group.strip():
        raise ConfigError(f"{where}.group must be a non-empty string")

    return TargetConfig(
        url=url,
        expected_statuses=expected,
        timeout=timeout,
        retries=retries,
        verify_ssl=verify_ssl,
        alert_threshold=alert_threshold,
        name=name.strip(),
        check_type=check_type,
        host=host,
        port=port,
        group=group.strip(),
        contains=contains,
        regex=regex,
        method=method,
        headers=headers,
        max_latency_ms=max_latency,
        ssl_warn_days=ssl_warn_days,
        enabled=enabled,
        follow_redirects=follow,
        user_agent=str(defaults.get("user_agent", USER_AGENT)),
    )


def parse_webhook(raw: Any, *, where: str = "webhook") -> Webhook:
    if isinstance(raw, str):
        url = raw
        kind = None
    elif isinstance(raw, dict):
        extra = set(raw) - _ALLOWED_WEBHOOK
        if extra:
            raise ConfigError(f"{where}: unknown key(s): {', '.join(sorted(extra))}")
        url = raw.get("url")
        kind = raw.get("type")
        if not isinstance(url, str):
            raise ConfigError(f"{where}.url must be a string")
    else:
        raise ConfigError(f"{where} must be a url string or a mapping")

    parts = urlsplit(url.strip())
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ConfigError(f"{where}: webhook url must be http(s): {url}")
    if kind is None:
        kind = infer_webhook_kind(url)
    if not isinstance(kind, str):
        raise ConfigError(f"{where}.type must be a string")
    kind = kind.lower()
    if kind not in {"generic", "slack", "discord"}:
        raise ConfigError(f"{where}.type must be generic, slack, or discord")
    return Webhook(url=url.strip(), kind=kind)


def infer_webhook_kind(url: str) -> str:
    host = urlsplit(url).netloc.lower()
    if "discord.com" in host or "discordapp.com" in host:
        return "discord"
    if host.endswith("slack.com") or host == "hooks.slack.com":
        return "slack"
    return "generic"


def validate_host(host: str, *, where: str) -> str:
    cleaned = host.strip()
    if cleaned.startswith("[") and cleaned.endswith("]"):
        cleaned = cleaned[1:-1]
    if not cleaned or cleaned.startswith("-") or _HOST_UNSAFE.search(cleaned):
        raise ConfigError(f"{where}: invalid host {host!r}")
    return cleaned


def split_host_port(spec: str, *, where: str = "tcp target") -> tuple[str, int]:
    text = spec.strip()
    if text.startswith("["):
        host, sep, rest = text[1:].partition("]")
        if sep != "]" or not rest.startswith(":"):
            raise ConfigError(f"{where}: expected [host]:port, got {spec!r}")
        port_text = rest[1:]
    else:
        if text.count(":") != 1:
            raise ConfigError(f"{where}: expected host:port, got {spec!r}")
        host, port_text = text.split(":", 1)
    host = validate_host(host, where=where)
    port = _as_int(port_text, f"{where}.port", minimum=1)
    if port > 65535:
        raise ConfigError(f"{where}.port must be <= 65535")
    return host, port


def _read_file(path: str) -> Any:
    file_path = Path(path)
    if not file_path.is_file():
        raise ConfigError(f"config file not found: {path}")
    ext = file_path.suffix.lower()
    try:
        if ext == ".json":
            with file_path.open("r", encoding="utf-8") as handle:
                return json.load(handle)
        if ext in {".yaml", ".yml"}:
            import yaml

            with file_path.open("r", encoding="utf-8") as handle:
                return yaml.safe_load(handle)
        if ext == ".toml":
            try:
                import tomllib
            except ModuleNotFoundError:  # pragma: no cover - Python 3.10
                import tomli as tomllib

            with file_path.open("rb") as handle:
                return tomllib.load(handle)
    except ConfigError:
        raise
    except Exception as exc:
        raise ConfigError(f"could not parse {path}: {exc}") from exc
    raise ConfigError(f"unsupported config type {ext or '(none)'}; use .json, .yaml, or .toml")


def _identity(raw: dict, where: str) -> tuple[str, str, str, int]:
    explicit = raw.get("type")
    url = raw.get("url")
    host = raw.get("host")
    port = raw.get("port")

    if explicit is None and isinstance(url, str):
        scheme = urlsplit(url).scheme.lower()
        if scheme in {"http", "https"}:
            explicit = "http"
        elif scheme == "tcp":
            explicit = "tcp"
        elif scheme == "ping":
            explicit = "ping"
        else:
            raise ConfigError(f"{where}: unsupported url scheme {scheme!r}")
    if explicit is None:
        raise ConfigError(f"{where}: set url or type")
    if not isinstance(explicit, str):
        raise ConfigError(f"{where}.type must be a string")
    explicit = explicit.lower()
    if explicit in {"http", "https"}:
        check_type = "http"
    elif explicit in {"tcp", "ping"}:
        check_type = explicit
    else:
        raise ConfigError(f"{where}.type must be http, tcp, or ping")

    if check_type == "http":
        if not isinstance(url, str) or not url.strip():
            raise ConfigError(f"{where}: http targets need a url")
        parts = urlsplit(url.strip())
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ConfigError(f"{where}: url must be http(s) with a host")
        return check_type, url.strip(), parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)

    if isinstance(url, str) and url.strip():
        parts = urlsplit(url.strip())
        if host is None:
            host = parts.hostname
        if port is None and check_type == "tcp":
            port = parts.port

    if not isinstance(host, str) or not host.strip():
        raise ConfigError(f"{where}: {check_type} targets need a host")
    host = validate_host(host, where=where)

    if check_type == "ping":
        return check_type, f"ping://{host}", host, 0

    if port is None:
        raise ConfigError(f"{where}: tcp targets need a port")
    port_num = _as_int(port, f"{where}.port", minimum=1)
    if port_num > 65535:
        raise ConfigError(f"{where}.port must be <= 65535")
    return check_type, f"tcp://{host}:{port_num}", host, port_num


def _expected_statuses(raw: dict, defaults: dict, where: str) -> list[int]:
    if "expected_statuses" in raw:
        value = raw["expected_statuses"]
    elif "expected_status" in raw:
        value = raw["expected_status"]
    elif "expected_statuses" in defaults:
        value = defaults["expected_statuses"]
    elif "expected_status" in defaults:
        value = defaults["expected_status"]
    else:
        value = [200]
    if isinstance(value, int) and not isinstance(value, bool):
        value = [value]
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{where}.expected_statuses must be a non-empty list of status codes")
    codes = []
    for item in value:
        code = _as_int(item, f"{where}.expected_statuses", minimum=100)
        if code > 599:
            raise ConfigError(f"{where}.expected_statuses codes must be <= 599")
        codes.append(code)
    return codes


def _contains(value: Any, where: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{where}.contains must be a string or a non-empty list of strings")
    needles = []
    for item in value:
        if not isinstance(item, str) or item == "":
            raise ConfigError(f"{where}.contains entries must be non-empty strings")
        needles.append(item)
    return needles


def _headers(value: Any, where: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{where}.headers must be a mapping")
    headers = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise ConfigError(f"{where}.headers keys must be strings")
        if isinstance(item, (dict, list)) or item is None:
            raise ConfigError(f"{where}.headers.{key} must be a scalar")
        headers[key] = str(item)
    return headers


def _default_name(check_type: str, url: str, host: str, port: int) -> str:
    if check_type == "http":
        return urlsplit(url).netloc or url
    if check_type == "tcp":
        return f"{host}:{port}"
    return host


def _pick(raw: dict, defaults: dict, key: str, fallback: Any) -> Any:
    if key in raw:
        return raw[key]
    if key in defaults:
        return defaults[key]
    return fallback


def _as_bool(value: Any, where: str) -> bool:
    if isinstance(value, bool):
        return value
    raise ConfigError(f"{where} must be true or false")


def _as_int(value: Any, where: str, *, minimum: int) -> int:
    if isinstance(value, bool) or isinstance(value, float):
        raise ConfigError(f"{where} must be an integer")
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        value = int(value.strip())
    if not isinstance(value, int):
        raise ConfigError(f"{where} must be an integer")
    if value < minimum:
        raise ConfigError(f"{where} must be >= {minimum}")
    return value


def _as_float(value: Any, where: str, *, minimum: float, exclusive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{where} must be a number")
    number = float(value)
    if exclusive:
        if number <= minimum:
            raise ConfigError(f"{where} must be > {minimum}")
    elif number < minimum:
        raise ConfigError(f"{where} must be >= {minimum}")
    return number
