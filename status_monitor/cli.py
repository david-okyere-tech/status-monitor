"""Command line interface.

`status-monitor --url ...` keeps the v4 flags. Subcommands add `check` (one
cycle), `report`, `validate`, and `demo`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from typing import Optional

from status_monitor import __version__
from status_monitor.config import (
    USER_AGENT,
    AppConfig,
    ConfigError,
    load_config,
    parse_target,
    parse_webhook,
    split_host_port,
)
from status_monitor.models import (
    DEFAULT_ALERT_THRESHOLD,
    DEFAULT_COOLDOWN_CHECKS,
    DEFAULT_CONCURRENCY,
    DEFAULT_LOG_DIR,
    DEFAULT_RETENTION_DAYS,
    DEFAULT_RETRIES,
    DEFAULT_TIMEOUT,
    DEFAULT_WEB_HOST,
    DEFAULT_WEB_PORT,
    SQLITE_FILENAME,
    Settings,
)
from status_monitor.stats import fmt_ms, fmt_pct, percentile, uptime_percent

_SUBCOMMANDS = {"run", "check", "report", "validate", "demo"}


def main(argv: Optional[list[str]] = None) -> None:
    load_dotenv()
    if argv is None:
        argv = sys.argv[1:]
    command, namespace = parse_args(list(argv))
    try:
        if command == "validate":
            _cmd_validate(namespace)
            return
        if command == "report":
            _cmd_report(namespace)
            return
        if command == "demo":
            _cmd_demo(namespace)
            return
        settings = settings_from_namespace(namespace, once=(command == "check" or namespace.once))
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)

    from status_monitor.monitor import run_loop

    try:
        code = asyncio.run(run_loop(settings))
    except KeyboardInterrupt:
        print("\n👋 Stopped.")
        code = 130
    sys.exit(code)


def parse_args(argv: list[str]) -> tuple[str, argparse.Namespace]:
    if argv and argv[0] in _SUBCOMMANDS:
        parser = _build_sub_parser()
        namespace = parser.parse_args(argv)
        return namespace.command, namespace
    parser = _build_monitor_parser("status-monitor")
    return "run", parser.parse_args(argv)


def settings_from_namespace(namespace: argparse.Namespace, *, once: bool = False) -> Settings:
    app = load_config(namespace.config) if namespace.config else AppConfig(user_agent=USER_AGENT)
    _reject_bad_numbers(namespace)
    targets = [target for target in app.targets if target.enabled]
    targets.extend(_cli_targets(namespace, app))
    seen: set[str] = set()
    for target in targets:
        if target.url in seen:
            raise ConfigError(f"duplicate target url: {target.url}")
        seen.add(target.url)
    if not targets:
        raise ConfigError("No targets specified — use --url (repeatable) and/or --config")

    if namespace.dashboard and namespace.plain:
        raise ConfigError("choose either --dashboard or --plain")

    interval = app.interval if namespace.interval is None else float(namespace.interval)
    email = namespace.email or app.email
    webhooks = list(app.webhooks)
    for hook in namespace.webhook or []:
        webhooks.append(parse_webhook(hook, where="--webhook"))
    env_hook = os.environ.get("WEBHOOK_URL")
    if env_hook:
        webhooks.append(parse_webhook(env_hook, where="WEBHOOK_URL"))

    cooldown = app.cooldown_checks if namespace.cooldown_checks is None else int(namespace.cooldown_checks)
    log_dir = namespace.log_dir or app.log_dir or DEFAULT_LOG_DIR
    concurrency = app.concurrency if namespace.concurrency is None else int(namespace.concurrency)
    retention = app.history_retention_days if namespace.retention_days is None else int(namespace.retention_days)
    web_port = DEFAULT_WEB_PORT if namespace.web_port is None else int(namespace.web_port)
    if not 1 <= web_port <= 65535:
        raise ConfigError("--web-port must be between 1 and 65535")

    return Settings(
        targets=targets,
        interval=interval,
        email=email,
        webhooks=webhooks,
        cooldown_checks=cooldown,
        log_dir=log_dir,
        once=once or bool(namespace.once),
        web=bool(namespace.web),
        web_host=namespace.web_host or DEFAULT_WEB_HOST,
        web_port=web_port,
        dashboard=bool(namespace.dashboard),
        quiet=bool(namespace.quiet),
        verbose=bool(namespace.verbose),
        concurrency=concurrency,
        retention_days=retention,
        user_agent=app.user_agent,
    )


def load_dotenv(path: str = ".env") -> None:
    """Load KEY=VALUE pairs that are not already set in the environment."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export ") :].strip()
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def _cmd_validate(namespace: argparse.Namespace) -> None:
    app = load_config(namespace.config)
    enabled = [target for target in app.targets if target.enabled]
    print(f"OK: {len(enabled)} target(s), interval {app.interval:g}s")
    for target in app.targets:
        flag = "" if target.enabled else " (disabled)"
        extras = []
        if target.contains:
            extras.append("contains " + ", ".join(target.contains))
        if target.regex:
            extras.append(f"regex /{target.regex}/")
        if target.ssl_warn_days and target.url.startswith("https"):
            extras.append(f"tls<{target.ssl_warn_days}d")
        if target.max_latency_ms:
            extras.append(f"slow>{target.max_latency_ms:.0f}ms")
        suffix = ("  " + "  ".join(extras)) if extras else ""
        print(f"  [{target.check_type}] {target.name}  {target.url}{suffix}{flag}")
    if app.email:
        print(f"  email: {app.email}")
    if app.webhooks:
        print(f"  webhooks: {len(app.webhooks)}")


def _cmd_report(namespace: argparse.Namespace) -> None:
    from status_monitor.history import HistoryStore

    path = os.path.join(namespace.log_dir, SQLITE_FILENAME)
    if not os.path.exists(path):
        print(f"No history yet at {path}")
        return
    since = None if namespace.hours <= 0 else time.time() - namespace.hours * 3600
    window = "all time" if since is None else f"last {namespace.hours:g}h"
    store = HistoryStore(namespace.log_dir)
    try:
        rows = []
        wanted = set(namespace.urls or [])
        for item in store.urls():
            if wanted and item["url"] not in wanted:
                continue
            counts = store.counts(item["url"], since)
            samples = store.latencies(item["url"], since, limit=5000)
            latest = store.latest(item["url"]) or {}
            total = sum(counts.values())
            rows.append(
                {
                    "name": item["name"],
                    "url": item["url"],
                    "type": item["check_type"],
                    "checks": total,
                    "uptime_pct": uptime_percent(counts.get("UP", 0), counts.get("WARNING", 0), counts.get("DOWN", 0)),
                    "p50": percentile(samples, 50),
                    "p95": percentile(samples, 95),
                    "p99": percentile(samples, 99),
                    "last_status": latest.get("status"),
                    "last_checked": latest.get("ts"),
                    "last_error": latest.get("error"),
                }
            )
    finally:
        store.close()

    if namespace.json:
        print(json.dumps({"window": window, "targets": rows}, indent=2))
        return
    if not rows:
        print(f"No checks in {path} for {window}")
        return
    print(f"Status report ({window})")
    for row in rows:
        print(f"\n  {row['name']}  [{row['type']}]")
        print(f"  {row['url']}")
        uptime = fmt_pct(row["uptime_pct"], digits=2)
        print(
            f"  checks {row['checks']} | uptime {uptime} | "
            f"p50 {fmt_ms(row['p50'])} | p95 {fmt_ms(row['p95'])} | p99 {fmt_ms(row['p99'])}"
        )
        if row["last_status"]:
            extra = f" — {row['last_error']}" if row["last_error"] else ""
            print(f"  last {row['last_status']} at {row['last_checked']}{extra}")


def _cmd_demo(namespace: argparse.Namespace) -> None:
    from status_monitor.demo import openssl_available, start_fixtures
    from status_monitor.monitor import run_loop

    if not openssl_available():
        print("error: demo needs openssl to mint local TLS certificates", file=sys.stderr)
        sys.exit(2)
    if namespace.interval is not None and namespace.interval <= 0:
        print("error: --interval must be > 0", file=sys.stderr)
        sys.exit(2)
    directory = namespace.log_dir or os.path.join(DEFAULT_LOG_DIR, "demo")
    os.makedirs(directory, exist_ok=True)
    fixtures = start_fixtures(os.path.join(directory, "certs"))
    code = 0
    try:
        web_port = DEFAULT_WEB_PORT if namespace.web_port is None else int(namespace.web_port)
        if not 1 <= web_port <= 65535:
            print("error: --web-port must be between 1 and 65535", file=sys.stderr)
            sys.exit(2)
        settings = Settings(
            targets=fixtures.targets,
            interval=5 if namespace.interval is None else float(namespace.interval),
            log_dir=directory,
            once=bool(namespace.once),
            web=not bool(namespace.no_web),
            web_host=namespace.web_host or DEFAULT_WEB_HOST,
            web_port=web_port,
            dashboard=bool(namespace.dashboard),
            quiet=bool(namespace.quiet),
            cooldown_checks=DEFAULT_COOLDOWN_CHECKS,
            concurrency=DEFAULT_CONCURRENCY,
            retention_days=DEFAULT_RETENTION_DAYS,
            user_agent=USER_AGENT,
        )
        print("Demo monitors are local processes (HTTP, TLS, TCP). Nothing leaves the machine.")
        code = asyncio.run(run_loop(settings))
    except KeyboardInterrupt:
        print("\n👋 Stopped.")
        code = 130
    finally:
        fixtures.stop()
    sys.exit(code)


def _cli_targets(namespace: argparse.Namespace, app: AppConfig) -> list:
    timeout = DEFAULT_TIMEOUT if namespace.timeout is None else float(namespace.timeout)
    retries = DEFAULT_RETRIES if namespace.retries is None else int(namespace.retries)
    threshold = DEFAULT_ALERT_THRESHOLD if namespace.alert_threshold is None else int(namespace.alert_threshold)
    expected = namespace.expected_status or [200]
    contains = namespace.contains or []
    defaults = {"user_agent": app.user_agent}
    targets = []
    for url in namespace.urls or []:
        raw = {
            "url": url,
            "timeout": timeout,
            "retries": retries,
            "expected_statuses": expected,
            "alert_threshold": threshold,
            "verify_ssl": not namespace.no_verify_ssl,
        }
        if contains:
            raw["contains"] = contains
        if namespace.ssl_warn_days:
            raw["ssl_warn_days"] = namespace.ssl_warn_days
        targets.append(parse_target(raw, defaults, where=f"url {url}"))
    for spec in namespace.tcp or []:
        host, port = split_host_port(spec, where=f"--tcp {spec}")
        targets.append(
            parse_target(
                {
                    "type": "tcp",
                    "host": host,
                    "port": port,
                    "timeout": timeout,
                    "retries": retries,
                    "alert_threshold": threshold,
                },
                defaults,
                where=f"--tcp {spec}",
            )
        )
    for host in namespace.ping or []:
        targets.append(
            parse_target(
                {
                    "type": "ping",
                    "host": host,
                    "timeout": timeout,
                    "retries": retries,
                    "alert_threshold": threshold,
                },
                defaults,
                where=f"--ping {host}",
            )
        )
    return targets


def _reject_bad_numbers(namespace: argparse.Namespace) -> None:
    if namespace.retries is not None and namespace.retries < 0:
        raise ConfigError("--retries must be >= 0")
    if namespace.timeout is not None and namespace.timeout <= 0:
        raise ConfigError("--timeout must be > 0")
    if namespace.interval is not None and namespace.interval <= 0:
        raise ConfigError("--interval must be > 0")
    if namespace.alert_threshold is not None and namespace.alert_threshold < 1:
        raise ConfigError("--alert-threshold must be >= 1")
    if namespace.cooldown_checks is not None and namespace.cooldown_checks < 0:
        raise ConfigError("--cooldown-checks must be >= 0")
    if namespace.concurrency is not None and namespace.concurrency < 1:
        raise ConfigError("--concurrency must be >= 1")
    if namespace.retention_days is not None and namespace.retention_days < 0:
        raise ConfigError("--retention-days must be >= 0")
    if namespace.ssl_warn_days is not None and namespace.ssl_warn_days < 0:
        raise ConfigError("--ssl-warn-days must be >= 0")


def _build_monitor_parser(prog: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description=f"Async Status Monitor v{__version__}",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        epilog=(
            "Subcommands: run, check, report, validate, demo. "
            "Example: status-monitor demo"
        ),
    )
    _add_monitor_arguments(parser)
    return parser


def _build_sub_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="status-monitor", description=f"Async Status Monitor v{__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Monitor until stopped", formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    _add_monitor_arguments(run)

    check = sub.add_parser("check", help="Run one cycle and exit", formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    _add_monitor_arguments(check)

    report = sub.add_parser("report", help="Print uptime and latency from history")
    report.add_argument("--log-dir", default=DEFAULT_LOG_DIR, help="Directory that holds history.sqlite")
    report.add_argument("--hours", type=float, default=24, help="Window in hours (0 = all history)")
    report.add_argument("--url", action="append", dest="urls", help="Limit the report to this URL (repeatable)")
    report.add_argument("--json", action="store_true", help="Print the report as JSON")

    validate = sub.add_parser("validate", help="Check a config file and list targets")
    validate.add_argument("--config", required=True, help="Path to a JSON, YAML, or TOML config")

    demo = sub.add_parser("demo", help="Monitor local demo services and open the status page")
    demo.add_argument("--interval", type=float, default=None, help="Seconds between cycles (default: 5)")
    demo.add_argument("--web-host", default=None)
    demo.add_argument("--web-port", type=int, default=None)
    demo.add_argument("--no-web", action="store_true", help="Skip the status page")
    demo.add_argument("--log-dir", default=None, help="Log directory (default: ./logs/demo)")
    demo.add_argument("--once", action="store_true", help="Run a single cycle")
    demo.add_argument("--dashboard", action="store_true", help="Live terminal dashboard")
    demo.add_argument("--quiet", action="store_true")
    return parser


def _add_monitor_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", help="JSON, YAML, or TOML config file")
    parser.add_argument("--url", action="append", dest="urls", help="HTTP(S) URL to monitor (repeatable)")
    parser.add_argument("--tcp", action="append", default=[], help="TCP target host:port (repeatable)")
    parser.add_argument("--ping", action="append", default=[], help="Host to ICMP ping (repeatable)")
    parser.add_argument("--interval", type=float, default=None, help=f"Seconds between cycles (default: config or {60})")
    parser.add_argument("--timeout", type=float, default=None, help="Timeout for CLI targets, in seconds (default: 10)")
    parser.add_argument("--retries", type=int, default=None, help="Retries after the first attempt for CLI targets (default: 2)")
    parser.add_argument(
        "--expected-status",
        action="append",
        dest="expected_status",
        type=int,
        help="Expected HTTP status for CLI URLs (repeatable, default: 200)",
    )
    parser.add_argument(
        "--alert-threshold",
        type=int,
        default=None,
        help="Consecutive DOWN checks before an alert, for CLI targets (default: 1)",
    )
    parser.add_argument("--email", default=None, help="Email address for DOWN and recovery alerts")
    parser.add_argument("--webhook", action="append", default=[], help="Webhook URL for alerts (repeatable)")
    parser.add_argument("--cooldown-checks", type=int, default=None, help="DOWN checks to suppress after recovery (default: 3)")
    parser.add_argument("--log-dir", default=None, help=f"Lock, state, log, and history directory (default: {DEFAULT_LOG_DIR})")
    parser.add_argument("--no-verify-ssl", action="store_true", help="Disable TLS verification for CLI URLs")
    parser.add_argument("--contains", action="append", default=None, help="Text that CLI URL bodies must include (repeatable)")
    parser.add_argument("--ssl-warn-days", type=int, default=None, help="Warn when a CLI URL certificate expires sooner than this")
    parser.add_argument("--once", action="store_true", help="Run a single check cycle and exit")
    parser.add_argument("--web", action="store_true", help="Serve the status page")
    parser.add_argument("--web-host", default=None, help=f"Status page bind address (default: {DEFAULT_WEB_HOST})")
    parser.add_argument("--web-port", type=int, default=None, help=f"Status page port (default: {DEFAULT_WEB_PORT})")
    parser.add_argument("--dashboard", action="store_true", help="Live terminal dashboard")
    parser.add_argument("--plain", action="store_true", help="Line-by-line console output (the default)")
    parser.add_argument("--quiet", action="store_true", help="Suppress per-check console lines")
    parser.add_argument("--verbose", action="store_true", help="Debug logging")
    parser.add_argument("--concurrency", type=int, default=None, help="Maximum in-flight checks (default: 50 or config)")
    parser.add_argument(
        "--retention-days",
        type=int,
        default=None,
        help="Delete SQLite rows older than this many days (default: 30, 0 keeps everything)",
    )

