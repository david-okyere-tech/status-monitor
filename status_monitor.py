"""Status Monitor - A website uptime monitoring tool.

Monitors one or more URLs at configurable intervals, logs results with
response times, and optionally alerts via email when a site goes DOWN.

Usage:
    python status_monitor.py --url https://example.com
    python status_monitor.py --url https://example.com --interval 30 --checks 10
    python status_monitor.py --url https://example.com --email you@gmail.com

Email alerting requires SMTP credentials set as environment variables:
    SMTP_USER  - Gmail address (e.g. yourbot@gmail.com)
    SMTP_PASS  - Gmail App Password (NOT your regular password)

    To generate a Gmail App Password:
      1. Go to https://myaccount.google.com/apppasswords
      2. Sign in, select "Mail" and your device, click Generate
      3. Use the 16-character password as SMTP_PASS
"""

from __future__ import annotations

import argparse
import json
import os
import smtplib
import sys
import time
from datetime import datetime
from email.message import EmailMessage
from typing import Optional
from urllib.parse import urlparse

import requests

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEFAULT_INTERVAL = 60          # seconds between checks
DEFAULT_TIMEOUT = 10           # HTTP request timeout in seconds
DEFAULT_CHECKS = 0             # 0 = run until stopped with Ctrl+C
DEFAULT_MAX_RETRIES = 2        # retries before declaring DOWN
DEFAULT_ALERT_THRESHOLD = 1    # consecutive DOWN checks before alerting


# ---------------------------------------------------------------------------
# URL validation
# ---------------------------------------------------------------------------
def is_valid_url(url: str) -> bool:
    """Validate a URL using urllib.parse.urlparse.

    Checks that the URL has a scheme (http/https) and a network location.
    This is more robust than simple startswith() checks.
    """
    try:
        result = urlparse(url)
        return all([
            result.scheme in ("http", "https"),
            result.netloc,           # must have a domain
        ])
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
class CheckResult:
    """Represents a single status check."""

    def __init__(self, url: str, status: str, status_code: Optional[int],
                 response_time_ms: Optional[float], error: Optional[str] = None):
        self.url = url
        self.timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.status = status            # UP / DOWN / WARNING
        self.status_code = status_code
        self.response_time_ms = response_time_ms
        self.error = error

    def to_dict(self) -> dict:
        return self.__dict__

    def log_line(self) -> str:
        """Format for log file — includes response time."""
        code = f" | Status: {self.status_code}" if self.status_code else ""
        rt = f" | Response: {self.response_time_ms:.0f}ms" if self.response_time_ms else ""
        err = f" | Error: {self.error}" if self.error else ""
        return f"[{self.timestamp}] {self.url} -> {self.status}{code}{rt}{err}"

    def console_line(self, alert_active: bool = False) -> str:
        """Format for console output with emoji.

        Args:
            alert_active: If True and status is DOWN, show the 🚨 icon.
                          If False and status is DOWN, show ⬇️ (below threshold).
        """
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
# Core checker
# ---------------------------------------------------------------------------
def check_url(url: str, timeout: int = DEFAULT_TIMEOUT,
              max_retries: int = DEFAULT_MAX_RETRIES,
              expected_statuses: Optional[list[int]] = None) -> CheckResult:
    """Send an HTTP GET to *url* and return a CheckResult.

    Retries up to *max_retries* times before declaring DOWN to avoid
    false alarms from momentary blips.

    Args:
        url: Target URL.
        timeout: HTTP request timeout in seconds.
        max_retries: How many attempts before declaring DOWN.
        expected_statuses: List of acceptable HTTP status codes.
                           Defaults to [200] if not provided.
    """
    if expected_statuses is None:
        expected_statuses = [200]

    last_error = None

    for attempt in range(1, max_retries + 1):
        try:
            start = time.monotonic()
            resp = requests.get(url, timeout=timeout, allow_redirects=True)
            elapsed_ms = (time.monotonic() - start) * 1000

            if resp.status_code in expected_statuses:
                return CheckResult(url, "UP", resp.status_code, elapsed_ms)
            else:
                expected_str = ", ".join(str(s) for s in expected_statuses)
                return CheckResult(
                    url, "WARNING", resp.status_code, elapsed_ms,
                    error=f"Expected {expected_str}, got {resp.status_code}",
                )

        except requests.exceptions.Timeout:
            last_error = "Request timed out"
        except requests.exceptions.ConnectionError:
            last_error = "Connection failed"
        except requests.exceptions.TooManyRedirects:
            last_error = "Too many redirects"
        except requests.exceptions.RequestException as exc:
            last_error = str(exc)

        if attempt < max_retries:
            time.sleep(1)  # brief pause before retry

    return CheckResult(url, "DOWN", None, None, error=last_error)


# ---------------------------------------------------------------------------
# Email alerting
# ---------------------------------------------------------------------------
def send_email_alert(result: CheckResult, recipient: str) -> None:
    """Send a DOWN alert email using SMTP credentials from environment variables.

    Required environment variables:
        SMTP_USER  - Gmail address (e.g. yourbot@gmail.com)
        SMTP_PASS  - Gmail App Password (16 chars, no spaces)

    To set them:
        export SMTP_USER="yourbot@gmail.com"
        export SMTP_PASS="abcd efgh ijkl mnop"   # or without spaces

    For Gmail, you MUST use an App Password, not your regular password.
    Generate one at: https://myaccount.google.com/apppasswords
    """
    smtp_user = os.environ.get("SMTP_USER", "").strip()
    smtp_pass = os.environ.get("SMTP_PASS", "").strip().replace(" ", "")

    if not smtp_user or not smtp_pass:
        print(f"  ⚠️  Email alert skipped: SMTP_USER and SMTP_PASS env vars not set")
        return

    msg = EmailMessage()
    msg["Subject"] = f"🚨 Status Monitor: {result.url} is DOWN"
    msg["From"] = smtp_user
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
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)
        print(f"  📧 Alert email sent to {recipient}")
    except smtplib.SMTPAuthenticationError:
        print(f"  ⚠️  Email failed: SMTP authentication error. Check SMTP_USER/SMTP_PASS.")
    except Exception as exc:
        print(f"  ⚠️  Email failed: {exc}")


# ---------------------------------------------------------------------------
# Summary report
# ---------------------------------------------------------------------------
def print_summary(results: list[CheckResult], urls: list[str]) -> None:
    """Print a summary report after monitoring completes.

    Shows per-URL: total checks, UP/WARNING/DOWN counts, effective uptime
    (UP + WARNING) / total, average/min/max response time, downtime periods.
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

        # Effective uptime: sites that are reachable (UP or WARNING) count as up
        reachable = up_count + warning_count
        uptime_pct = (reachable / total) * 100 if total else 0

        # Response times (only for checks that got a response)
        response_times = [r.response_time_ms for r in url_results if r.response_time_ms is not None]
        avg_rt = sum(response_times) / len(response_times) if response_times else 0
        min_rt = min(response_times) if response_times else 0
        max_rt = max(response_times) if response_times else 0

        # Downtime periods (only DOWN status)
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
        print(f"  Uptime (UP+WARN): {uptime_pct:.1f}%")
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
  python status_monitor.py --url https://example.com --email you@gmail.com
  python status_monitor.py --url https://example.com --expected-status 200 --expected-status 201
  python status_monitor.py --url https://example.com --alert-threshold 3

Email setup (Gmail):
  export SMTP_USER="yourbot@gmail.com"
  export SMTP_PASS="your-16-char-app-password"
  python status_monitor.py --url https://example.com --email you@gmail.com
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
        "--retries", type=int, default=DEFAULT_MAX_RETRIES,
        help=f"Retries before declaring DOWN (default: {DEFAULT_MAX_RETRIES})",
    )
    p.add_argument(
        "--email", type=str, default=None,
        help="Email address to send DOWN alerts to (requires SMTP_USER/SMTP_PASS env vars)",
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
        "--log-dir", type=str, default="./logs",
        help="Directory for log files (default: ./logs)",
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

    # Validate URLs using urllib.parse
    for url in args.urls:
        if not is_valid_url(url):
            print(f"❌ Invalid URL: {url}")
            print(f"   URLs must include a scheme (http:// or https://) and a domain.")
            sys.exit(1)

    # Validate interval
    if args.interval < 1:
        print("❌ Interval must be at least 1 second.")
        sys.exit(1)

    # Validate alert threshold
    if args.alert_threshold < 1:
        print("❌ Alert threshold must be at least 1.")
        sys.exit(1)

    urls = args.urls
    interval = args.interval
    max_checks = args.checks if args.checks > 0 else None   # None = infinite
    timeout = args.timeout
    max_retries = args.retries
    expected_statuses = args.expected_statuses if args.expected_statuses else [200]
    alert_threshold = args.alert_threshold
    email_recipient = args.email
    log_dir = os.path.abspath(args.log_dir)
    verbose = args.verbose

    # Set up log directory and open log file once for the entire run
    os.makedirs(log_dir, exist_ok=True)
    log_filename = os.path.join(log_dir, f"status_log_{datetime.now().strftime('%Y-%m-%d')}.txt")

    log_file = open(log_filename, "a", encoding="utf-8")

    # Write session header
    header = (
        f"\n{'=' * 60}\n"
        f"Status Monitor Session\n"
        f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"URLs: {', '.join(urls)}\n"
        f"Interval: {interval}s | Timeout: {timeout}s | Retries: {max_retries}\n"
        f"Expected statuses: {', '.join(str(s) for s in expected_statuses)}\n"
        f"Alert threshold: {alert_threshold} consecutive DOWN(s)\n"
        f"{'=' * 60}\n"
    )
    log_file.write(header)
    log_file.flush()

    # Open JSONL history file once for the entire run (append mode)
    history_path = os.path.join(log_dir, "history.jsonl")
    history_file = open(history_path, "a", encoding="utf-8")

    print(f"\n🔍 Monitoring {len(urls)} URL(s) every {interval}s")
    if max_checks:
        print(f"   Running {max_checks} check(s)")
    else:
        print(f"   Running until Ctrl+C")
    if email_recipient:
        print(f"   📧 Email alerts → {email_recipient} (threshold: {alert_threshold})")
    print(f"   📄 Log file: {log_filename}")
    print(f"   📊 History: {history_path}\n")

    # Track results for summary
    results: list[CheckResult] = []
    check_count = 0

    # Track consecutive failures per URL for alert threshold
    consecutive_down: dict[str, int] = {url: 0 for url in urls}

    try:
        while True:
            check_count += 1

            for url in urls:
                result = check_url(
                    url,
                    timeout=timeout,
                    max_retries=max_retries,
                    expected_statuses=expected_statuses,
                )
                results.append(result)

                # Track consecutive failures
                if result.status == "DOWN":
                    consecutive_down[url] += 1
                else:
                    consecutive_down[url] = 0

                # Determine if alert threshold is met
                alert_active = consecutive_down[url] >= alert_threshold

                # Console output
                print(result.console_line(alert_active=alert_active))

                # Log file output
                log_file.write(result.log_line() + "\n")
                log_file.flush()

                # Append one line to JSONL history (no read-rewrite)
                history_file.write(json.dumps(result.to_dict()) + "\n")
                history_file.flush()

                # Email alert only when threshold is met (on the exact check that crosses it)
                if alert_active and consecutive_down[url] == alert_threshold and email_recipient:
                    send_email_alert(result, email_recipient)

            # Check if we've reached the requested number of checks
            if max_checks and check_count >= max_checks:
                break

            # Sleep in 1-second increments for responsive Ctrl+C
            for _ in range(interval):
                time.sleep(1)

    except KeyboardInterrupt:
        print("\n\n⏹  Stopped by Ctrl+C")

    finally:
        # Always print summary and close files cleanly
        print_summary(results, urls)

        # Write session footer to log
        footer = (
            f"\n{'─' * 60}\n"
            f"Session ended: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"Total checks: {check_count}\n"
            f"{'=' * 60}\n"
        )
        log_file.write(footer)
        log_file.close()
        history_file.close()
        print(f"📄 Log saved to: {log_filename}")
        print(f"📊 History saved to: {history_path}")


if __name__ == "__main__":
    main()
