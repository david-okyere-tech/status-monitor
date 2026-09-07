"""
Status Monitor v4.0 (Production Refactor)
- Async IO (httpx) instead of Threads
- Persistent Alert State (survives crashes)
- O(1) Memory footprint (no more deque hoarding)
- OS-level File Locking (fcntl) instead of PID files
- Exponential Backoff with Jitter
- Rotating File Handlers for logs
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
    """O(1) Memory Statistics Tracker"""
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
            except Exception:
                self.states = {}  # Corrupt state, start fresh

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

    def update(self, result: CheckResult, threshold: int, cooldown_checks: int = 3) -> tuple[bool, bool]:
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
                state["cooldown_remaining"] = cooldown_checks
                is_recovery = True
            state["consecutive_down"] = 0
            state["alert_fired"] = False
            state["outage_start"] = None
            state["cooldown_remaining"] = 0 if not is_recovery else state["cooldown_remaining"]

        else:  # WARNING
            state["consecutive_down"] = 0

        self.save()
        return should_alert, is_recovery

# ---------------------------------------------------------------------------
# Async Core Logic
# ---------------------------------------------------------------------------
async def check_url_async(client: httpx.AsyncClient, target: TargetConfig) -> CheckResult:
    max_attempts = target.retries + 1
    last_error = None
    
    for attempt in range(1, max_attempts + 1):
        try:
            start = time.monotonic()
            resp = await client.get(target.url, timeout=target.timeout, follow_redirects=True, verify=target.verify_ssl)
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
        await aiosmtplib.send(
            msg, hostname=host, port=port, username=user, password=passwd,
            start_tls=use_tls, validate_certs=True
        )
        print(f"  📧 {'Recovery' if is_recovery else 'Alert'} email sent to {recipient}")
    except Exception as exc:
        print(f"  ⚠️  Email failed: {exc}")

# ---------------------------------------------------------------------------
# Process Locking & Logging
# ---------------------------------------------------------------------------
def acquire_lock(log_dir: str):
    lock_path = os.path.join(log_dir, LOCK_FILENAME)
    lock_file = open(lock_path, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
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
# Main Execution Loop
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

    print(f"🔍 Monitoring {len(targets)} URL(s) every {interval}s (Async)")
    if email: print(f"   📧 Alerts → {email}")
    print(f"   🔒 Lock: Active | 📊 History: {history_path}\n")

    try:
        async with httpx.AsyncClient() as client:
            while True:
                check_start = time.monotonic()
                tasks = [check_url_async(client, t) for t in targets]
                results = await asyncio.gather(*tasks)
                
                for result, target in zip(results, targets):
                    stats[result.url].update(result)
                    should_alert, is_recovery = state_mgr.update(result, target.alert_threshold)
                    
                    # Console & Log
                    icon = "✅" if result.status == "UP" else ("🚨" if result.status == "DOWN" else "⚠️")
                    line = f"{icon} [{result.timestamp}] {result.url}: {result.status} ({result.status_code}) {result.error or ''}"
                    print(line)
                    logger.info(line)
                    
                    # JSONL
                    history_file.write(json.dumps(result.to_dict()) + "\n")
                    history_file.flush()
                    
                    # Alerts
                    if email:
                        if should_alert:
                            await send_email_async(result, email)
                        elif is_recovery:
                            await send_email_async(result, email, outage_start=state_mgr.get(result.url).get("outage_start"), is_recovery=True)

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

def main():
    parser = argparse.ArgumentParser(description="Async Status Monitor v4.0")
    parser.add_argument("--config", type=str, help="Path to JSON config file")
    parser.add_argument("--url", action="append", dest="urls", help
