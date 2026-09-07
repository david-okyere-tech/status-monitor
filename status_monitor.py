"""
Status Monitor v4.1 (Production Refactor + fixes)
- Async IO (httpx) instead of Threads
- Persistent Alert State (survives crashes)
- Bounded memory (downtime_periods capped; no more deque hoarding)
- OS-level File Locking (fcntl) instead of PID files
- Exponential Backoff with Jitter
- Rotating File Handlers for logs
- State/history writes batched once per cycle instead of once per URL
- SSL verification set at the httpx.Client level (works with modern httpx)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import fcntl
import random
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime
from typing import Optional, Any
from dataclasses import dataclass, field, asdict

import httpx
import aiosmtplib
from email.message import EmailMessage

# ---------------------------------------------------------------------------
# Constants & Data Models
# ---------------------------------------------------------------------------
SCHEMA_VERSION = 4
DEFAULT_INTERVAL = 60
DEFAULT_TIMEOUT = 10
DEFAULT_RETRIES = 2
DEFAULT_ALERT_THRESHOLD = 1
DEFAULT_LOG_DIR = "./logs"
LOCK_FILENAME = "status_monitor.lock"
STATE_FILENAME = ".state.json"
MAX_DOWNTIME_PERIODS = 100  # keep memory bounded on long-running processes
EMAIL_SEND_TIMEOUT = 15  # seconds — bounds SMTP send so a hung server can't stall the check loop

@dataclass
class TargetConfig:
    url: str
    expected_statuses: list[int] = field(default_factory=lambda: [200])
    timeout: int = DEFAULT_TIMEOUT
    retries: int = DEFAULT_RETRIES
    verify_ssl: bool = True
    alert_threshold: int = DEFAULT_ALERT_THRESHOLD

@dataclass
class CheckResult:
    url: str
    timestamp: str
    status: str  # UP, DOWN, WARNING
    status_code: Optional[int]
    response_time_ms: Optional[float]
    error: Optional[str]
    attempt: int

    def to_dict(self) -> dict:
        return {
            "v": SCHEMA_VERSION, "url": self.url, "timestamp": self.timestamp,
            "status": self.status, "status_code": self.status_code,
            "response_time_ms": round(self.response_time_ms, 1) if self.response_time_ms else None,
            "error": self.error, "attempt": self.attempt,
        }

@dataclass
class URLStats:
    """Bounded-memory statistics tracker"""
    total: int = 0
    up: int = 0
    down: int = 0
    warning: int = 0
    rt_sum: float = 0.0
    rt_min: float = float('inf')
    rt_max: float = 0.0
    rt_count: int = 0
    downtime_periods: list = field(default_factory=list)
    in_downtime: bool = False
    downtime_start: Optional[str] = None

    def update(self, result: CheckResult):
        self.total += 1
        if result.status == "UP": self.up += 1
        elif result.status == "DOWN": self.down += 1
        else: self.warning += 1

        if result.response_time_ms is not None:
            self.rt_sum += result.response_time_ms
            self.rt_count += 1
            if result.response_time_ms < self.rt_min: self.rt_min = result.response_time_ms
            if result.response_time_ms > self.rt_max: self.rt_max = result.response_time_ms

        if result.status == "DOWN" and not self.in_downtime:
            self.in_downtime = True
            self.downtime_start = result.timestamp
        elif result.status != "DOWN" and self.in_downtime:
            self.in_downtime = False
            self.downtime_periods.append((self.downtime_start, result.timestamp))
            if len(self.downtime_periods) > MAX_DOWNTIME_PERIODS:
                self.downtime_periods = self.downtime_periods[-MAX_DOWNTIME_PERIODS:]

    def finalize(self, last_timestamp: str):
        if self.in_downtime and self.downtime_start:
            self.downtime_periods.append((self.downtime_start, last_timestamp))

# ---------------------------------------------------------------------------
# Persistent Alert State Manager (Survives Crashes)
# ---------------------------------------------------------------------------
class AlertStateManager:
    def __init__(self, state_file: str):
        self.state_file = state_file
        self.states: dict[str, dict] = {}
        self.load()

    def load(self):
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, 'r') as f:
                    self.states = json.load(f)
            except Exception as exc:
                # Corrupt state file: this silently discarding was the old
                # behavior, which quietly defeats the "crash-safe" guarantee
                # (you'd lose in-progress outage tracking with no signal).
                # Log loudly, preserve the bad file for inspection, and
                # start fresh rather than overwrite the evidence.
                quarantine_path = self.state_file + ".corrupt"
                try:
                    os.replace(self.state_file, quarantine_path)
                except OSError:
                    quarantine_path = None
                msg = (
                    f"⚠️  State file {self.state_file} is corrupt ({exc}) — "
                    f"starting with fresh alert state."
                    + (f" Bad file saved to {quarantine_path}." if quarantine_path else "")
                )
                print(msg)
                logging.getLogger("status_monitor").warning(msg)
                self.states = {}

    def save(self):
        temp_file = self.state_file + '.tmp'
        with open(temp_file, 'w') as f:
            json.dump(self.states, f)
        os.replace(temp_file, self.state_file)  # Atomic write

    def get(self, url: str) -> dict:
        if url not in self.states:
            self.states[url] = {
                "consecutive_down": 0, "alert_fired": False,
                "outage_start": None, "cooldown_remaining": 0
            }
        return self.states[url]

    def update(self, result: CheckResult, threshold: int, cooldown_checks: int = 3, persist: bool = True) -> tuple[bool, bool]:
        """
        persist=False lets callers batch saves — e.g. call update() for every
        URL in a check cycle, then call save() once at the end of the cycle,
        instead of hitting disk once per URL per cycle.
        """
        state = self.get(result.url)
        should_alert, is_recovery = False, False

        if result.status == "DOWN":
            if state["cooldown_remaining"] > 0:
                state["cooldown_remaining"] -= 1
                state["consecutive_down"] += 1
                if not state["outage_start"]: state["outage_start"] = result.timestamp
            else:
                state["consecutive_down"] += 1
                if not state["outage_start"]: state["outage_start"] = result.timestamp
                if state["consecutive_down"] >= threshold and not state["alert_fired"]:
                    state["alert_fired"] = True
                    should_alert = True

        elif result.status == "UP":
            was_in_outage = state["alert_fired"]
            if was_in_outage and state["outage_start"]:
                # This UP is the recovery itself: start the cooldown window.
                is_recovery = True
                state["cooldown_remaining"] = cooldown_checks
            else:
                # A plain UP (no outage just ended). Clear any leftover
                # cooldown so it can't silently suppress a *future*,
                # unrelated outage — without this, a cooldown left over
                # from an old flap would still be >0 when a genuinely new
                # DOWN streak starts, and would wrongly delay that alert.
                state["cooldown_remaining"] = 0
            state["consecutive_down"] = 0
            state["alert_fired"] = False
            state["outage_start"] = None

        else:  # WARNING
            state["consecutive_down"] = 0

        if persist:
            self.save()
        return should_alert, is_recovery

# ---------------------------------------------------------------------------
# Async Core Logic
# ---------------------------------------------------------------------------
async def check_url_async(client: httpx.AsyncClient, target: TargetConfig) -> CheckResult:
    """
    NOTE: `verify` is intentionally NOT passed here. SSL verification is set
    at the httpx.AsyncClient construction level (see run_loop) because
    per-request `verify=` was deprecated/removed in newer httpx releases.
    """
    max_attempts = target.retries + 1
    last_error = None

    for attempt in range(1, max_attempts + 1):
        try:
            start = time.monotonic()
            resp = await client.get(target.url, timeout=target.timeout, follow_redirects=True)
            elapsed_ms = (time.monotonic() - start) * 1000

            if resp.status_code in target.expected_statuses:
                return CheckResult(target.url, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "UP", resp.status_code, elapsed_ms, None, attempt)
            else:
                last_error = f"Expected {target.expected_statuses}, got {resp.status_code}"
                if attempt < max_attempts:
                    backoff = min(30, (2 ** attempt) + random.uniform(0, 1))
                    await asyncio.sleep(backoff)
                    continue
                return CheckResult(target.url, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "WARNING", resp.status_code, elapsed_ms, last_error, attempt)

        except httpx.ConnectError: last_error = "Connection failed"
        except httpx.TimeoutException: last_error = "Request timed out"
        except httpx.TooManyRedirects: last_error = "Too many redirects"
        except httpx.RequestError as exc: last_error = str(exc)

        if attempt < max_attempts:
            backoff = min(30, (2 ** attempt) + random.uniform(0, 1))
            await asyncio.sleep(backoff)

    return CheckResult(target.url, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "DOWN", None, None, last_error, max_attempts)

async def send_email_async(result: CheckResult, recipient: str, outage_start: Optional[str] = None, is_recovery: bool = False):
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER", "")
    passwd = os.environ.get("SMTP_PASS", "").replace(" ", "")
    use_tls = os.environ.get("SMTP_TLS", "true").lower() != "false"

    if not user or not passwd: return

    msg = EmailMessage()
    msg["From"] = user
    msg["To"] = recipient

    if is_recovery:
        msg["Subject"] = f"✅ Status Monitor: {result.url} is back UP"
        msg.set_content(f"URL: {result.url}\nOutage started: {outage_start}\nRecovered at: {result.timestamp}\n")
    else:
        msg["Subject"] = f"🚨 Status Monitor: {result.url} is DOWN"
        msg.set_content(f"URL: {result.url}\nTime: {result.timestamp}\nError: {result.error}\n")

    try:
        await asyncio.wait_for(
            aiosmtplib.send(
                msg, hostname=host, port=port, username=user, password=passwd,
                start_tls=use_tls, validate_certs=True
            ),
            timeout=EMAIL_SEND_TIMEOUT,
        )
        print(f"  📧 {'Recovery' if is_recovery else 'Alert'} email sent to {recipient}")
    except asyncio.TimeoutError:
        print(f"  ⚠️  Email failed: SMTP send timed out after {EMAIL_SEND_TIMEOUT}s")
    except Exception as exc:
        print(f"  ⚠️  Email failed: {exc}")

# ---------------------------------------------------------------------------
# Process Locking & Logging
# ---------------------------------------------------------------------------
def acquire_lock(log_dir: str):
    """
    Opens in append mode (not 'w') so we never truncate another running
    instance's lock file content before we know whether we actually hold
    the lock. Only truncate + write our own PID after flock succeeds.
    """
    lock_path = os.path.join(log_dir, LOCK_FILENAME)
    lock_file = open(lock_path, "a+")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_file.seek(0)
        lock_file.truncate()
        lock_file.write(str(os.getpid()))
        lock_file.flush()
        return lock_file
    except IOError:
        lock_file.close()
        return None

def setup_logging(log_dir: str) -> logging.Logger:
    os.makedirs(log_dir, exist_ok=True)
    logger = logging.getLogger("status_monitor")
    logger.setLevel(logging.INFO)

    # Rotating file handler prevents disk exhaustion (5MB max, keep 3 backups)
    handler = RotatingFileHandler(
        os.path.join(log_dir, "monitor.log"), maxBytes=5*1024*1024, backupCount=3
    )
    handler.setFormatter(logging.Formatter('%(message)s'))
    logger.addHandler(handler)
    return logger

# ---------------------------------------------------------------------------
# Config Loading
# ---------------------------------------------------------------------------
def load_config(config_path: str) -> tuple[dict, list[TargetConfig]]:
    with open(config_path, 'r') as f:
        data = json.load(f)

    global_conf = data.get("global", {})
    targets = []
    for t in data.get("targets", []):
        targets.append(TargetConfig(
            url=t["url"],
            expected_statuses=t.get("expected_statuses", global_conf.get("expected_statuses", [200])),
            timeout=t.get("timeout", global_conf.get("timeout", DEFAULT_TIMEOUT)),
            retries=t.get("retries", global_conf.get("retries", DEFAULT_RETRIES)),
            verify_ssl=t.get("verify_ssl", global_conf.get("verify_ssl", True)),
            alert_threshold=t.get("alert_threshold", global_conf.get("alert_threshold", DEFAULT_ALERT_THRESHOLD))
        ))
    return global_conf, targets

# ---------------------------------------------------------------------------
# Main Execution Loop
# ---------------------------------------------------------------------------
async def run_loop(targets: list[TargetConfig], interval: int, email: Optional[str], log_dir: str):
    lock_file = acquire_lock(log_dir)
    if not lock_file:
        print(f"❌ Another instance is already running (Lockfile: {os.path.join(log_dir, LOCK_FILENAME)})")
        sys.exit(1)

    logger = setup_logging(log_dir)
    state_mgr = AlertStateManager(os.path.join(log_dir, STATE_FILENAME))
    stats: dict[str, URLStats] = {t.url: URLStats() for t in targets}

    history_path = os.path.join(log_dir, "history.jsonl")
    history_file = open(history_path, "a", encoding="utf-8")
    history_buffer: list[str] = []

    print(f"🔍 Monitoring {len(targets)} URL(s) every {interval}s (Async)")
    if email: print(f"   📧 Alerts → {email}")
    print(f"   🔒 Lock: Active | 📊 History: {history_path}\n")

    try:
        # Two clients so per-target verify_ssl still works, without passing
        # a deprecated `verify=` kwarg on every request.
        async with httpx.AsyncClient(verify=True) as client_verified, \
                   httpx.AsyncClient(verify=False) as client_unverified:

            def client_for(target: TargetConfig) -> httpx.AsyncClient:
                return client_verified if target.verify_ssl else client_unverified

            while True:
                check_start = time.monotonic()
                tasks = [check_url_async(client_for(t), t) for t in targets]
                results = await asyncio.gather(*tasks)

                for result, target in zip(results, targets):
                    stats[result.url].update(result)
                    should_alert, is_recovery = state_mgr.update(
                        result, target.alert_threshold, persist=False
                    )

                    # Console & Log
                    icon = "✅" if result.status == "UP" else ("🚨" if result.status == "DOWN" else "⚠️")
                    line = f"{icon} [{result.timestamp}] {result.url}: {result.status} ({result.status_code}) {result.error or ''}"
                    print(line)
                    logger.info(line)

                    # Buffer JSONL — one write per cycle, not one per URL
                    history_buffer.append(json.dumps(result.to_dict()))

                    # Alerts
                    if email:
                        if should_alert:
                            await send_email_async(result, email)
                        elif is_recovery:
                            await send_email_async(result, email, outage_start=state_mgr.get(result.url).get("outage_start"), is_recovery=True)

                # Batched disk writes — once per cycle, not once per URL.
                # Both are blocking file I/O; run them in a thread so they
                # can't stall the event loop (and therefore the next
                # cycle's URL checks) while writing to a slow disk.
                await asyncio.to_thread(state_mgr.save)
                if history_buffer:
                    lines = "\n".join(history_buffer) + "\n"
                    history_buffer.clear()

                    def _write_history(data: str = lines):
                        history_file.write(data)
                        history_file.flush()

                    await asyncio.to_thread(_write_history)

                elapsed = time.monotonic() - check_start
                await asyncio.sleep(max(0, interval - elapsed))

    except asyncio.CancelledError:
        pass
    finally:
        history_file.close()
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()

        # Print Summary
        print("\n" + "=" * 60 + "\n  MONITORING SUMMARY\n" + "=" * 60)
        for url, s in stats.items():
            s.finalize(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            uptime = ((s.up + s.warning) / s.total * 100) if s.total else 0
            avg_rt = (s.rt_sum / s.rt_count) if s.rt_count else 0
            print(f"\n  📍 {url}\n  Total: {s.total} | UP: {s.up} | WARN: {s.warning} | DOWN: {s.down} | Uptime: {uptime:.1f}%")
            if s.rt_count: print(f"  Response: avg {avg_rt:.0f}ms | min {s.rt_min:.0f}ms | max {s.rt_max:.0f}ms")

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _targets_from_cli_args(args: argparse.Namespace) -> list[TargetConfig]:
    targets = []
    for url in (args.urls or []):
        targets.append(TargetConfig(
            url=url,
            expected_statuses=args.expected_status or [200],
            timeout=args.timeout,
            retries=args.retries,
            verify_ssl=not args.no_verify_ssl,
            alert_threshold=args.alert_threshold,
        ))
    return targets

def main():
    parser = argparse.ArgumentParser(description="Async Status Monitor v4.1")
    parser.add_argument("--config", type=str, help="Path to a JSON config file (see README)")
    parser.add_argument("--url", action="append", dest="urls",
                         help="URL to monitor (repeatable; combines with --config if both given)")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL,
                         help=f"Seconds between check cycles (default: {DEFAULT_INTERVAL})")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                         help=f"HTTP request timeout in seconds (default: {DEFAULT_TIMEOUT})")
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES,
                         help=f"Retries after the first attempt (default: {DEFAULT_RETRIES})")
    parser.add_argument("--expected-status", action="append", dest="expected_status", type=int,
                         help="Expected HTTP status code (repeatable, default: 200)")
    parser.add_argument("--alert-threshold", type=int, default=DEFAULT_ALERT_THRESHOLD,
                         help=f"Consecutive DOWN checks before alerting (default: {DEFAULT_ALERT_THRESHOLD})")
    parser.add_argument("--email", type=str, default=None,
                         help="Email address for DOWN/recovery alerts")
    parser.add_argument("--log-dir", type=str, default=DEFAULT_LOG_DIR,
                         help=f"Directory for lock/state/log/history files (default: {DEFAULT_LOG_DIR})")
    parser.add_argument("--no-verify-ssl", action="store_true",
                         help="Disable SSL certificate verification")
    args = parser.parse_args()

    # Basic input validation — previously a negative --retries silently
    # produced max_attempts=0, so check_url_async's retry loop never ran
    # and every check returned DOWN/error=None without actually trying.
    if args.retries < 0:
        parser.error("--retries must be >= 0")
    if args.timeout <= 0:
        parser.error("--timeout must be > 0")
    if args.interval <= 0:
        parser.error("--interval must be > 0")
    if args.alert_threshold < 1:
        parser.error("--alert-threshold must be >= 1")

    targets: list[TargetConfig] = []
    if args.config:
        _, config_targets = load_config(args.config)
        targets.extend(config_targets)
    targets.extend(_targets_from_cli_args(args))

    if not targets:
        parser.error("No targets specified — use --url (repeatable) and/or --config")

    os.makedirs(args.log_dir, exist_ok=True)

    try:
        asyncio.run(run_loop(targets, args.interval, args.email, args.log_dir))
    except KeyboardInterrupt:
        print("\n👋 Stopped.")

if __name__ == "__main__":
    main()
