"""Status Monitor - A website uptime monitoring tool.

Monitors one or more URLs at configurable intervals, logs results with
response times, and optionally alerts via email when a site goes DOWN.

Usage:
    python status_monitor.py --url https://example.com
    python status_monitor.py --url https://example.com --interval 30 --checks 10
    python status_monitor.py --url https://example.com --email you@example.com

Email alerting requires SMTP credentials set as environment variables:
    SMTP_HOST  - SMTP server hostname (default: smtp.gmail.com)
    SMTP_PORT  - SMTP server port (default: 587)
    SMTP_USER  - Email address for authentication
    SMTP_PASS  - Email password or App Password
    SMTP_TLS   - Use STARTTLS (default: true)

    For Gmail, you MUST use an App Password, not your regular password.
    Generate one at: https://myaccount.google.com/apppasswords
"""

from __future__ import annotations

import argparse
import json
import os
import smtplib
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from email.message import EmailMessage
from typing import Optional
from urllib.parse import urlparse

import requests

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SCHEMA_VERSION = 3
DEFAULT_INTERVAL = 60
DEFAULT_TIMEOUT = 10
DEFAULT_CHECKS = 0               # 0 = run until Ctrl+C
DEFAULT_RETRIES = 2              # number of RETRIES after the first attempt
DEFAULT_ALERT_THRESHOLD = 1
DEFAULT_LOG_DIR = "./logs"
DEFAULT_MAX_RESULTS = 10000      # cap in-memory results for long-running processes
PID_FILENAME = "status_monitor.pid"


# ---------------------------------------------------------------------------
# URL validation
# ---------------------------------------------------------------------------
def is_valid_url(url: str) -> bool:
    """Validate a URL using urllib.parse.urlparse."""
    try:
        result = urlparse(url)
        return result.scheme in ("http", "https") and bool(result.netloc)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
class CheckResult:
    """Represents a single status check."""

    def __init__(
        self,
        url: str,
        status: str,
        status_code: Optional[int],
        response_time_ms: Optional[float],
        error: Optional[str] = None,
        attempt: int = 1,
    ):
        self.url = url
        self.timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.status = status            # UP / DOWN / WARNING
        self.status_code = status_code
        self.response_time_ms = response_time_ms
        self.error = error
        self.attempt = attempt

    def to_dict(self) -> dict:
        """Explicit serialization — no __dict__ leaking internals."""
        return {
            "v": SCHEMA_VERSION,
            "url": self.url,
            "timestamp": self.timestamp,
            "status": self.status,
            "status_code": self.status_code,
            "response_time_ms": round(self.response_time_ms, 1) if self.response_time_ms is not None else None,
            "error": self.error,
            "attempt": self.attempt,
        }

    def log_line(self) -> str:
        code = f" | Status: {self.status_code}" if self.status_code else ""
        rt = f" | Response: {self.response_time_ms:.0f}ms" if self.response_time_ms else ""
        err = f" | Error: {self.error}" if self.error else ""
        return f"[{self.timestamp}] {self.url} -> {self.status}{code}{rt}{err}"

    def console_line(self, alert_active: bool = False) -> str:
        code = f" ({self.status_code})" if self.status_code else ""
        rt = f" {self.response_time_ms:.0f}ms" if self.response_time_ms else ""
        err = f" - {self.error}" if self.error else ""
        if self.status == "UP":
            icon = "✅"
        elif self.status == "DOWN":
            icon = "🚨" if alert_active else "⬇️"
        else:
            icon = "⚠️"
        return f"{icon} [{self.timestamp}] {self.url}: {self.status}{code}{rt}{err}"


# ---------------------------------------------------------------------------
# Core checker — retries both connection failures AND unexpected status codes
# ---------------------------------------------------------------------------
def check_url(
    url: str,
    timeout: int = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
    expected_statuses: Optional[list[int]] = None,
    verify_ssl: bool = True,
) -> CheckResult:
    """Send HTTP GET to *url* and return a CheckResult.

    Retries up to *retries* times after the first attempt for BOTH
    connection failures and unexpected status codes. A 503 is just as
    likely to be transient as a connection timeout.

    Args:
        url: Target URL.
        timeout: HTTP request timeout in seconds.
        retries: Number of retries AFTER the first attempt (total = retries + 1).
        expected_statuses: Acceptable HTTP status codes. Defaults to [200].
        verify_ssl: Whether to verify TLS certificates.
    """
    if expected_statuses is None:
        expected_statuses = [200]

    expected_str = ", ".join(str(s) for s in expected_statuses)
    max_attempts = retries + 1
    last_error = None

    for attempt in range(1, max_attempts + 1):
        try:
            start = time.monotonic()
            resp = requests.get(
                url, timeout=timeout, allow_redirects=True, verify=verify_ssl
            )
            elapsed_ms = (time.monotonic() - start) * 1000

            if resp.status_code in expected_statuses:
                return CheckResult(url, "UP", resp.status_code, elapsed_ms, attempt=attempt)
            else:
                last_error = f"Expected {expected_str}, got {resp.status_code}"
                # Retry unexpected status codes, same as connection errors
                if attempt < max_attempts:
                    time.sleep(1)
                    continue
                # All attempts exhausted with unexpected status
                return CheckResult(
                    url, "WARNING", resp.status_code, elapsed_ms,
                    error=last_error, attempt=attempt,
                )

        except requests.exceptions.SSLError as exc:
            last_error = f"SSL error: {exc}"
            break  # SSL errors won't fix themselves with retries
        except requests.exceptions.Timeout:
            last_error = "Request timed out"
        except requests.exceptions.ConnectionError:
            last_error = "Connection failed"
        except requests.exceptions.TooManyRedirects:
            last_error = "Too many redirects"
        except requests.exceptions.RequestException as exc:
            last_error = str(exc)

        if attempt < max_attempts:
            time.sleep(1)

    return CheckResult(url, "DOWN", None, None, error=last_error, attempt=max_attempts)


# ---------------------------------------------------------------------------
# Parallel URL checker
# ---------------------------------------------------------------------------
def check_urls_parallel(
    urls: list[str],
    timeout: int,
    retries: int,
    expected_statuses: list[int],
    verify_ssl: bool,
    max_workers: Optional[int] = None,
) -> list[CheckResult]:
    """Check all URLs concurrently using a thread pool.

    This prevents a single slow/dead URL from blocking the entire
    monitoring loop. Each URL gets its own thread, so a 10s timeout
    on one URL doesn't delay the others.

    Args:
        max_workers: Thread pool size. Defaults to min(len(urls), 10).
    """
    if max_workers is None:
        max_workers = min(len(urls), 10)

    results: list[CheckResult] = []

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                check_url, url, timeout, retries, expected_statuses, verify_ssl
            ): url
            for url in urls
        }
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as exc:
                url = futures[future]
                results.append(
                    CheckResult(url, "DOWN", None, None, error=f"Thread error: {exc}")
                )

    # Return in original URL order for consistent output
    url_order = {url: i for i, url in enumerate(urls)}
    results.sort(key=lambda r: url_order.get(r.url, 0))
    return results


# ---------------------------------------------------------------------------
# Alert state tracker — proper outage lifecycle
# ---------------------------------------------------------------------------
class AlertState:
    """Tracks per-URL alert state to handle the full outage lifecycle.

    - Alert once when threshold is crossed (not on every check)
    - Send recovery notification when site comes back up
    - Cooldown prevents alert spam during flapping
    - WARNING is treated as "not DOWN" — it resets the consecutive-down
      counter but does NOT trigger recovery (only a UP after a confirmed
      outage does). This prevents a flapping 500→200→500 pattern from
      generating spurious recovery emails.

    Note: State is in-memory only. Process restarts reset alert state.
    After a restart, the threshold counter starts from zero, so a
    persistent outage will be re-detected after `threshold` checks.
    """

    def __init__(self, urls: list[str], threshold: int, cooldown_checks: int = 3):
        self.threshold = threshold
        self.cooldown_checks = cooldown_checks
        self.consecutive_down: dict[str, int] = {url: 0 for url in urls}
        self.alert_fired: dict[str, bool] = {url: False for url in urls}
        self.outage_start: dict[str, Optional[str]] = {url: None for url in urls}
        self.cooldown_remaining: dict[str, int] = {url: 0 for url in urls}
        # Track outage_start across cooldown so recovery emails work correctly
        self._pending_outage_start: dict[str, Optional[str]] = {url: None for url in urls}

    def update(self, result: CheckResult) -> tuple[bool, bool]:
        """Process a check result and return (should_alert, is_recovery).

        should_alert: True if this check just crossed the threshold.
        is_recovery: True if the site just came back UP after a confirmed outage.
        """
        url = result.url

        if result.status == "DOWN":
            # Decrement cooldown if active
            if self.cooldown_remaining[url] > 0:
                self.cooldown_remaining[url] -= 1

            self.consecutive_down[url] += 1

            if self.outage_start[url] is None:
                self.outage_start[url] = result.timestamp

            # Fire alert only once per outage — when threshold is first crossed
            if self.consecutive_down[url] >= self.threshold and not self.alert_fired[url]:
                self.alert_fired[url] = True
                return True, False

            return False, False

        elif result.status == "UP":
            # UP means the site is genuinely working.
            # Check if we need to send recovery after a confirmed outage.
            was_in_outage = self.alert_fired[url]
            outage_start = self.outage_start[url]

            # Reset all state
            self.consecutive_down[url] = 0
            self.alert_fired[url] = False
            self.outage_start[url] = None
            self._pending_outage_start[url] = None

            if was_in_outage and outage_start is not None:
                # Start cooldown to prevent flapping spam
                self.cooldown_remaining[url] = self.cooldown_checks
                return False, True

            # Also clear cooldown if site is UP and wasn't in outage
            self.cooldown_remaining[url] = 0
            return False, False

        else:
            # WARNING — site is reachable but returning unexpected status.
            # This is NOT the same as UP: the site might be degraded.
            # Reset the consecutive DOWN counter (site is reachable) but
            # do NOT trigger recovery or reset outage tracking, because
            # the site isn't actually healthy.
            self.consecutive_down[url] = 0

            # If there's no active outage, do nothing
            if not self.alert_fired[url] and self.outage_start[url] is None:
                return False, False

            # If there IS an active outage, WARNING doesn't end it —
            # the site is still degraded. No recovery email.
            return False, False


# ---------------------------------------------------------------------------
# Email alerting — configurable SMTP, not hardcoded Gmail
# ---------------------------------------------------------------------------
def _get_smtp_config(
    smtp_host: Optional[str] = None,
    smtp_port: Optional[int] = None,
    smtp_user: Optional[str] = None,
    smtp_pass: Optional[str] = None,
    use_tls: Optional[bool] = None,
) -> tuple[str, int, str, str, bool]:
    """Resolve SMTP config from args or environment variables."""
    host = smtp_host or os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = smtp_port or int(os.environ.get("SMTP_PORT", "587"))
    user = (smtp_user or os.environ.get("SMTP_USER", "")).strip()
    raw_pass = smtp_pass or os.environ.get("SMTP_PASS", "")
    tls = use_tls if use_tls is not None else (
        os.environ.get("SMTP_TLS", "true").lower() != "false"
    )

    # Warn if spaces are being stripped from password
    if raw_pass and " " in raw_pass:
        print(f"  ⚠️  Note: Spaces removed from SMTP_PASS. If auth fails, check the password.")
        raw_pass = raw_pass.replace(" ", "")

    return host, port, user, raw_pass, tls


def send_email_alert(
    result: CheckResult,
    recipient: str,
    smtp_host: Optional[str] = None,
    smtp_port: Optional[int] = None,
    smtp_user: Optional[str] = None,
    smtp_pass: Optional[str] = None,
    use_tls: Optional[bool] = None,
) -> None:
    """Send a DOWN alert email using SMTP credentials from environment variables.

    Environment variables (all optional, with sensible defaults):
        SMTP_HOST  - SMTP server (default: smtp.gmail.com)
        SMTP_PORT  - SMTP port (default: 587)
        SMTP_USER  - Login email address (required)
        SMTP_PASS  - Login password or App Password (required)
        SMTP_TLS   - Use STARTTLS: "true" or "false" (default: true)
    """
    host, port, user, passwd, tls = _get_smtp_config(
        smtp_host, smtp_port, smtp_user, smtp_pass, use_tls
    )

    if not user or not passwd:
        print(f"  ⚠️  Email alert skipped: SMTP_USER and SMTP_PASS env vars not set")
        return

    msg = EmailMessage()
    msg["Subject"] = f"🚨 Status Monitor: {result.url} is DOWN"
    msg["From"] = user
    msg["To"] = recipient
    msg.set_content(
        f"Website Down Alert\n"
        f"{'=' * 40}\n\n"
        f"URL: {result.url}\n"
        f"Time: {result.timestamp}\n"
        f"Status: {result.status}\n"
        f"Error: {result.error or 'Unknown'}\n"
    )

    try:
        with smtplib.SMTP(host, port) as server:
            if tls:
                server.starttls()
            server.login(user, passwd)
            server.send_message(msg)
        print(f"  📧 Alert email sent to {recipient}")
    except smtplib.SMTPAuthenticationError:
        print(f"  ⚠️  Email failed: SMTP auth error. Check SMTP_USER/SMTP_PASS for {host}:{port}")
    except Exception as exc:
        print(f"  ⚠️  Email failed: {exc}")


def send_recovery_email(
    url: str,
    outage_start: str,
    recovery_time: str,
    recipient: str,
    smtp_host: Optional[str] = None,
    smtp_port: Optional[int] = None,
    smtp_user: Optional[str] = None,
    smtp_pass: Optional[str] = None,
    use_tls: Optional[bool] = None,
) -> None:
    """Send a recovery notification when a site comes back UP.

    Failures are logged but not treated as critical — the down alert
    is the important one. Recovery is best-effort.
    """
    host, port, user, passwd, tls = _get_smtp_config(
        smtp_host, smtp_port, smtp_user, smtp_pass, use_tls
    )

    if not user or not passwd:
        return  # Already warned in the down alert path

    msg = EmailMessage()
    msg["Subject"] = f"✅ Status Monitor: {url} is back UP"
    msg["From"] = user
    msg["To"] = recipient
    msg.set_content(
        f"Site Recovery Alert\n"
        f"{'=' * 40}\n\n"
        f"URL: {url}\n"
        f"Outage started: {outage_start}\n"
        f"Recovered at: {recovery_time}\n"
    )

    try:
        with smtplib.SMTP(host, port) as server:
            if tls:
                server.starttls()
            server.login(user, passwd)
            server.send_message(msg)
        print(f"  📧 Recovery email sent to {recipient}")
    except smtplib.SMTPAuthenticationError:
        print(f"  ⚠️  Recovery email failed: SMTP auth error for {host}:{port}")
    except Exception as exc:
        print(f"  ⚠️  Recovery email failed (non-critical): {exc}")


# ---------------------------------------------------------------------------
# Summary report
# ---------------------------------------------------------------------------
def print_summary(
    results: list[CheckResult],
    urls: list[str],
    count_warnings_as_up: bool = True,
) -> None:
    """Print a summary report after monitoring completes.

    Args:
        count_warnings_as_up: If True, WARNING counts as "reachable" in uptime %.
                              If False, only UP counts.
    """
    if not results:
        return

    print("\n" + "=" * 60)
    print("  MONITORING SUMMARY")
    print("=" * 60)

    for url in urls:
        url_results = [r for r in results if r.url == url]
        if not url_results:
            continue

        total = len(url_results)
        up_count = sum(1 for r in url_results if r.status == "UP")
        warning_count = sum(1 for r in url_results if r.status == "WARNING")
        down_count = sum(1 for r in url_results if r.status == "DOWN")

        if count_warnings_as_up:
            reachable = up_count + warning_count
            uptime_label = "Uptime (UP+WARN)"
        else:
            reachable = up_count
            uptime_label = "Uptime (UP only)"
        uptime_pct = (reachable / total) * 100 if total else 0

        response_times = [
            r.response_time_ms for r in url_results if r.response_time_ms is not None
        ]
        avg_rt = sum(response_times) / len(response_times) if response_times else 0
        min_rt = min(response_times) if response_times else 0
        max_rt = max(response_times) if response_times else 0

        # Downtime periods
        downtime_periods = []
        in_downtime = False
        downtime_start = None
        for r in url_results:
            if r.status == "DOWN" and not in_downtime:
                downtime_start = r.timestamp
                in_downtime = True
            elif r.status != "DOWN" and in_downtime:
                downtime_periods.append((downtime_start, r.timestamp))
                in_downtime = False
        if in_downtime:
            downtime_periods.append((downtime_start, url_results[-1].timestamp))

        print(f"\n  📍 {url}")
        print(f"  {'─' * 50}")
        print(f"  Total checks:     {total}")
        print(f"  UP: {up_count}  |  WARNING: {warning_count}  |  DOWN: {down_count}")
        print(f"  {uptime_label}: {uptime_pct:.1f}%")
        if response_times:
            print(f"  Response time:    avg {avg_rt:.0f}ms  |  min {min_rt:.0f}ms  |  max {max_rt:.0f}ms")
        else:
            print(f"  Response time:    N/A (all checks failed)")

        if downtime_periods:
            print(f"  Downtime periods:")
            for start, end in downtime_periods:
                print(f"    • {start} → {end}")
        else:
            print(f"  Downtime periods: None 🎉")

    print("\n" + "=" * 60 + "\n")


# ---------------------------------------------------------------------------
# PID file management
# ---------------------------------------------------------------------------
def write_pid_file(log_dir: str) -> None:
    """Write a PID file so other processes can find this monitor."""
    pid_path = os.path.join(log_dir, PID_FILENAME)
    with open(pid_path, "w") as f:
        f.write(str(os.getpid()))


def remove_pid_file(log_dir: str) -> None:
    """Clean up the PID file on exit."""
    pid_path = os.path.join(log_dir, PID_FILENAME)
    try:
        os.remove(pid_path)
    except FileNotFoundError:
        pass


def is_already_running(log_dir: str) -> Optional[int]:
    """Check if another instance using the same log directory is running.

    Note: This check is per-log-directory, not per-host. Two instances
    with different --log-dir values will not detect each other. If you
    need to prevent any duplicate, use a shared log directory.
    """
    pid_path = os.path.join(log_dir, PID_FILENAME)
    if not os.path.exists(pid_path):
        return None

    try:
        with open(pid_path) as f:
            pid = int(f.read().strip())

        # Check if the process actually exists
        os.kill(pid, 0)  # Raises OSError if process doesn't exist
        return pid
    except (ValueError, OSError, PermissionError):
        # Stale PID file — clean it up
        try:
            os.remove(pid_path)
        except FileNotFoundError:
            pass
        return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Website Status Monitor — check uptime, track response times, alert on downtime",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  python status_monitor.py --url https://example.com
  python status_monitor.py --url https://example.com --interval 30 --checks 10
  python status_monitor.py --url https://example.com --url https://google.com
  python status_monitor.py --url https://example.com --email you@example.com
  python status_monitor.py --url https://example.com --expected-status 200 --expected-status 201
  python status_monitor.py --url https://example.com --alert-threshold 3

Email setup:
  export SMTP_HOST="smtp.gmail.com"    # optional, defaults to Gmail
  export SMTP_PORT="587"               # optional, defaults to 587
  export SMTP_USER="yourbot@gmail.com"
  export SMTP_PASS="your-16-char-app-password"
  export SMTP_TLS="true"               # optional, defaults to true
  python status_monitor.py --url https://example.com --email you@example.com

Systemd:
  See status-monitor.service in this repo for running as a system service.
""",
    )
    p.add_argument(
        "--url", action="append", dest="urls", required=True,
        help="URL(s) to monitor — use --url multiple times for several sites",
    )
    p.add_argument(
        "--interval", type=int, default=DEFAULT_INTERVAL,
        help=f"Seconds between checks (default: {DEFAULT_INTERVAL})",
    )
    p.add_argument(
        "--checks", type=int, default=DEFAULT_CHECKS,
        help="Number of checks to run (default: 0 = run until Ctrl+C)",
    )
    p.add_argument(
        "--timeout", type=int, default=DEFAULT_TIMEOUT,
        help=f"HTTP request timeout in seconds (default: {DEFAULT_TIMEOUT})",
    )
    p.add_argument(
        "--retries", type=int, default=DEFAULT_RETRIES,
        help=f"Retries AFTER the first attempt (default: {DEFAULT_RETRIES}, total attempts: 3)",
    )
    p.add_argument(
        "--email", type=str, default=None,
        help="Email address to send DOWN/recovery alerts to",
    )
    p.add_argument(
        "--expected-status", action="append", dest="expected_statuses",
        type=int, default=None,
        help="Expected HTTP status code(s) — use multiple times (default: 200)",
    )
    p.add_argument(
        "--alert-threshold", type=int, default=DEFAULT_ALERT_THRESHOLD,
        help=f"Consecutive DOWN checks before sending alert (default: {DEFAULT_ALERT_THRESHOLD})",
    )
    p.add_argument(
        "--log-dir", type=str, default=DEFAULT_LOG_DIR,
        help=f"Directory for log files (default: {DEFAULT_LOG_DIR})",
    )
    p.add_argument(
        "--no-verify-ssl", action="store_true",
        help="Disable SSL certificate verification (useful for self-signed certs)",
    )
    p.add_argument(
        "--strict-uptime", action="store_true",
        help="Only count UP (not WARNING) in uptime percentage",
    )
    p.add_argument(
        "-v", "--verbose", action="store_true",
        help="Enable verbose output",
    )
    return p


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    # Validate URLs
    for url in args.urls:
        if not is_valid_url(url):
            print(f"❌ Invalid URL: {url}")
            print(f"   URLs must include a scheme (http:// or https://) and a domain.")
            sys.exit(1)

    # Validate numeric args
    if args.interval < 1:
        print("❌ Interval must be at least 1 second.")
        sys.exit(1)
    if args.alert_threshold < 1:
        print("❌ Alert threshold must be at least 1.")
        sys.exit(1)

    urls = args.urls
    interval = args.interval
    max_checks = args.checks if args.checks > 0 else None
    timeout = args.timeout
    retries = args.retries
    expected_statuses = args.expected_statuses if args.expected_statuses else [200]
    alert_threshold = args.alert_threshold
    email_recipient = args.email
    log_dir = os.path.abspath(args.log_dir)
    verify_ssl = not args.no_verify_ssl
    count_warnings_as_up = not args.strict_uptime
    verbose = args.verbose

    # Check for existing instance using the same log directory
    existing_pid = is_already_running(log_dir)
    if existing_pid:
        print(f"❌ Another instance is already running with this log dir (PID {existing_pid}).")
        print(f"   If this is stale, delete {os.path.join(log_dir, PID_FILENAME)}")
        sys.exit(1)

    # Set up log directory
    os.makedirs(log_dir, exist_ok=True)

    # Initialize before try so finally can always reference them
    log_file = None
    history_file = None
    log_filename = ""
    history_path = ""
    results: list[CheckResult] = []
    check_count = 0

    try:
        # Open log and history files inside the try block
        log_filename = os.path.join(
            log_dir, f"status_log_{datetime.now().strftime('%Y-%m-%d')}.txt"
        )
        log_file = open(log_filename, "a", encoding="utf-8")

        history_path = os.path.join(log_dir, "history.jsonl")
        history_file = open(history_path, "a", encoding="utf-8")

        # Write PID file
        write_pid_file(log_dir)

        # Write session header
        header = (
            f"\n{'=' * 60}\n"
            f"Status Monitor Session\n"
            f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"PID: {os.getpid()}\n"
            f"URLs: {', '.join(urls)}\n"
            f"Interval: {interval}s | Timeout: {timeout}s | Retries: {retries}\n"
            f"Expected statuses: {', '.join(str(s) for s in expected_statuses)}\n"
            f"Alert threshold: {alert_threshold} consecutive DOWN(s)\n"
            f"SSL verify: {verify_ssl}\n"
            f"{'=' * 60}\n"
        )
        log_file.write(header)
        log_file.flush()

        print(f"\n🔍 Monitoring {len(urls)} URL(s) every {interval}s")
        if max_checks:
            print(f"   Running {max_checks} check(s)")
        else:
            print(f"   Running until Ctrl+C")
        if email_recipient:
            print(f"   📧 Alerts → {email_recipient} (threshold: {alert_threshold})")
        print(f"   📄 Log: {log_filename}")
        print(f"   📊 History: {history_path}")
        print(f"   🔑 PID: {os.getpid()}\n")

        # Alert state tracker
        alert_state = AlertState(urls, threshold=alert_threshold)

        # Store the outage start for recovery emails (AlertState resets it
        # on recovery, so we capture it before calling update())
        last_outage_start: dict[str, Optional[str]] = {url: None for url in urls}

        while True:
            check_start = time.monotonic()
            check_count += 1

            # Check all URLs in parallel
            check_results = check_urls_parallel(
                urls, timeout, retries, expected_statuses, verify_ssl
            )

            for result in check_results:
                # Capture outage_start before update() resets it
                last_outage_start[result.url] = alert_state.outage_start.get(result.url)

                # Update alert state
                should_alert, is_recovery = alert_state.update(result)
                alert_active = alert_state.alert_fired.get(result.url, False)

                # Append to results (cap to prevent unbounded memory growth)
                results.append(result)
                if len(results) > DEFAULT_MAX_RESULTS:
                    results = results[-DEFAULT_MAX_RESULTS:]

                # Console output
                print(result.console_line(alert_active=alert_active))

                # Log file output
                log_file.write(result.log_line() + "\n")
                log_file.flush()

                # JSONL history (append-only, schema-versioned)
                history_file.write(json.dumps(result.to_dict()) + "\n")
                history_file.flush()

                # DOWN alert — fire once per outage
                if should_alert and email_recipient:
                    send_email_alert(result, email_recipient)

                # Recovery notification (only on UP after confirmed outage)
                if is_recovery and email_recipient:
                    outage_start = last_outage_start.get(result.url) or "unknown"
                    send_recovery_email(
                        result.url,
                        outage_start,
                        result.timestamp,
                        email_recipient,
                    )

            # Check if we've reached the requested number of checks
            if max_checks and check_count >= max_checks:
                break

            # Sleep only the remaining time to maintain accurate intervals
            elapsed = time.monotonic() - check_start
            sleep_time = max(0, interval - elapsed)

            if verbose:
                print(f"   ⏱  Check took {elapsed:.1f}s, sleeping {sleep_time:.1f}s")

            # Sleep in 1-second increments for responsive Ctrl+C
            slept = 0.0
            while slept < sleep_time:
                chunk = min(1.0, sleep_time - slept)
                time.sleep(chunk)
                slept += chunk

    except KeyboardInterrupt:
        print("\n\n⏹  Stopped by Ctrl+C")

    finally:
        # Always print summary
        print_summary(results, urls, count_warnings_as_up=count_warnings_as_up)

        # Close files safely — check they exist first
        if log_file is not None:
            try:
                footer = (
                    f"\n{'─' * 60}\n"
                    f"Session ended: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"Total checks: {check_count}\n"
                    f"{'=' * 60}\n"
                )
                log_file.write(footer)
                log_file.close()
                print(f"📄 Log saved to: {log_filename}")
            except Exception:
                pass

        if history_file is not None:
            try:
                history_file.close()
                print(f"📊 History saved to: {history_path}")
            except Exception:
                pass

        # Clean up PID file
        remove_pid_file(log_dir)


if __name__ == "__main__":
    main()
