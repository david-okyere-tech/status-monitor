"""
Status Monitor - A website uptime monitoring tool.

Monitors one or more URLs at configurable intervals, logs results,
and supports alerting via console, email, or webhook.

Usage:
    python status_monitor.py                          # interactive mode
    python status_monitor.py --config config.yaml     # config file mode
    python status_monitor.py -u https://example.com   # quick single URL
"""

import argparse
import json
import logging
import os
import signal
import smtplib
import sys
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Optional

import requests
import yaml

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEFAULT_INTERVAL = 60          # seconds between checks
DEFAULT_TIMEOUT = 10           # HTTP request timeout
DEFAULT_MAX_RETRIES = 2        # retries before declaring DOWN
LOG_DIR = Path("logs")
HISTORY_FILE = Path("history.json")

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
def setup_logging(verbose: bool = False) -> logging.Logger:
    """Configure structured console + file logging."""
    logger = logging.getLogger("status_monitor")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)-7s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    # File handler
    LOG_DIR.mkdir(exist_ok=True)
    fh = logging.FileHandler(
        LOG_DIR / f"monitor_{datetime.now().strftime('%Y%m%d')}.log"
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    return logger


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
class CheckResult:
    """Represents a single status check."""

    def __init__(self, url: str, status: str, status_code: Optional[int],
                 response_time_ms: Optional[float], error: Optional[str] = None):
        self.url = url
        self.timestamp = datetime.now(timezone.utc).isoformat()
        self.status = status            # UP / DOWN / WARNING
        self.status_code = status_code
        self.response_time_ms = response_time_ms
        self.error = error

    def to_dict(self) -> dict:
        return self.__dict__

    def __str__(self) -> str:
        code = f" ({self.status_code})" if self.status_code else ""
        rt = f" {self.response_time_ms:.0f}ms" if self.response_time_ms else ""
        err = f" - {self.error}" if self.error else ""
        return f"[{self.timestamp}] {self.url}: {self.status}{code}{rt}{err}"


# ---------------------------------------------------------------------------
# Core checker
# ---------------------------------------------------------------------------
def check_url(url: str, timeout: int = DEFAULT_TIMEOUT,
              max_retries: int = DEFAULT_MAX_RETRIES,
              expected_status: int = 200) -> CheckResult:
    """
    Send an HTTP GET to *url* and return a CheckResult.
    Retries up to *max_retries* times before declaring DOWN.
    """
    last_error = None

    for attempt in range(1, max_retries + 1):
        try:
            start = time.monotonic()
            resp = requests.get(url, timeout=timeout, allow_redirects=True)
            elapsed_ms = (time.monotonic() - start) * 1000

            if resp.status_code == expected_status:
                return CheckResult(url, "UP", resp.status_code, elapsed_ms)
            else:
                return CheckResult(
                    url, "WARNING", resp.status_code, elapsed_ms,
                    error=f"Expected {expected_status}, got {resp.status_code}",
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
# History persistence
# ---------------------------------------------------------------------------
def save_result(result: CheckResult) -> None:
    """Append a result to the JSON history file."""
    history = []
    if HISTORY_FILE.exists():
        try:
            history = json.loads(HISTORY_FILE.read_text())
        except (json.JSONDecodeError, ValueError):
            history = []

    history.append(result.to_dict())
    HISTORY_FILE.write_text(json.dumps(history, indent=2))


# ---------------------------------------------------------------------------
# Alerting
# ---------------------------------------------------------------------------
class Alerter:
    """Base alerter — just logs."""

    def __init__(self, logger: logging.Logger):
        self.logger = logger

    def send(self, result: CheckResult) -> None:
        if result.status == "DOWN":
            self.logger.warning(f"🚨 ALERT: {result}")
        elif result.status == "WARNING":
            self.logger.warning(f"⚠️  {result}")
        else:
            self.logger.info(f"✅ {result}")


class WebhookAlerter(Alerter):
    """POST check results to a webhook URL."""

    def __init__(self, logger: logging.Logger, url: str):
        super().__init__(logger)
        self.webhook_url = url

    def send(self, result: CheckResult) -> None:
        super().send(result)
        if result.status in ("DOWN", "WARNING"):
            try:
                requests.post(
                    self.webhook_url,
                    json=result.to_dict(),
                    timeout=5,
                )
            except requests.RequestException as exc:
                self.logger.error(f"Webhook delivery failed: {exc}")


class EmailAlerter(Alerter):
    """Send email alerts on downtime."""

    def __init__(self, logger: logging.Logger, smtp_host: str, smtp_port: int,
                 sender: str, recipient: str,
                 username: Optional[str] = None,
                 password: Optional[str] = None,
                 use_tls: bool = True):
        super().__init__(logger)
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.sender = sender
        self.recipient = recipient
        self.username = username
        self.password = password
        self.use_tls = use_tls

    def send(self, result: CheckResult) -> None:
        super().send(result)
        if result.status != "DOWN":
            return

        msg = EmailMessage()
        msg["Subject"] = f"🚨 Status Monitor Alert: {result.url} is DOWN"
        msg["From"] = self.sender
        msg["To"] = self.recipient
        msg.set_content(str(result))

        try:
            with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
                if self.use_tls:
                    server.starttls()
                if self.username and self.password:
                    server.login(self.username, self.password)
                server.send_message(msg)
        except Exception as exc:
            self.logger.error(f"Email delivery failed: {exc}")


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "urls": [],
    "interval": DEFAULT_INTERVAL,
    "timeout": DEFAULT_TIMEOUT,
    "max_retries": DEFAULT_MAX_RETRIES,
    "expected_status": 200,
    "alerts": {
        "webhook": None,
        "email": None,
    },
}


def load_config(path: str) -> dict:
    """Load and merge config from a YAML file."""
    cfg = DEFAULT_CONFIG.copy()
    if os.path.exists(path):
        with open(path) as f:
            user_cfg = yaml.safe_load(f) or {}
        cfg.update(user_cfg)
    return cfg


# ---------------------------------------------------------------------------
# Monitor loop
# ---------------------------------------------------------------------------
class Monitor:
    """Orchestrates periodic checks for one or more URLs."""

    def __init__(self, config: dict, logger: logging.Logger):
        self.config = config
        self.logger = logger
        self.running = True
        self.alerters: list[Alerter] = [Alerter(logger)]

        # Set up alerters from config
        alerts = config.get("alerts", {})
        if alerts.get("webhook"):
            self.alerters.append(WebhookAlerter(logger, alerts["webhook"]))

        email_cfg = alerts.get("email")
        if email_cfg:
            self.alerters.append(EmailAlerter(
                logger,
                smtp_host=email_cfg["smtp_host"],
                smtp_port=email_cfg.get("smtp_port", 587),
                sender=email_cfg["sender"],
                recipient=email_cfg["recipient"],
                username=email_cfg.get("username"),
                password=email_cfg.get("password"),
                use_tls=email_cfg.get("use_tls", True),
            ))

        # Graceful shutdown
        signal.signal(signal.SIGINT, self._shutdown)
        signal.signal(signal.SIGTERM, self._shutdown)

    def _shutdown(self, signum, frame):
        self.logger.info("Shutdown signal received, stopping…")
        self.running = False

    def run(self) -> None:
        """Run the monitoring loop until stopped."""
        urls = self.config.get("urls", [])
        interval = self.config.get("interval", DEFAULT_INTERVAL)

        if not urls:
            self.logger.error("No URLs configured. Add URLs to config.yaml or use -u flag.")
            return

        self.logger.info(
            f"Monitoring {len(urls)} URL(s) every {interval}s — press Ctrl+C to stop"
        )

        while self.running:
            for url in urls:
                result = check_url(
                    url,
                    timeout=self.config.get("timeout", DEFAULT_TIMEOUT),
                    max_retries=self.config.get("max_retries", DEFAULT_MAX_RETRIES),
                    expected_status=self.config.get("expected_status", 200),
                )
                save_result(result)
                for alerter in self.alerters:
                    alerter.send(result)

            # Sleep in small increments so Ctrl+C is responsive
            for _ in range(interval):
                if not self.running:
                    break
                time.sleep(1)

        self.logger.info("Monitor stopped.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Website Status Monitor — monitor uptime with alerting",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  python status_monitor.py -u https://example.com
  python status_monitor.py -u https://example.com -u https://google.com
  python status_monitor.py --config config.yaml
  python status_monitor.py --config config.yaml --interval 30
""",
    )
    p.add_argument(
        "-u", "--url", action="append", dest="urls",
        help="URL(s) to monitor (can specify multiple times)",
    )
    p.add_argument(
        "-c", "--config", default="config.yaml",
        help="Path to config YAML file (default: config.yaml)",
    )
    p.add_argument(
        "-i", "--interval", type=int, default=None,
        help="Check interval in seconds (overrides config)",
    )
    p.add_argument(
        "-t", "--timeout", type=int, default=None,
        help="HTTP request timeout in seconds",
    )
    p.add_argument(
        "-r", "--max-retries", type=int, default=None,
        help="Max retries before declaring DOWN",
    )
    p.add_argument(
        "-v", "--verbose", action="store_true",
        help="Enable debug-level logging",
    )
    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    logger = setup_logging(verbose=args.verbose)

    # Load config file (creates default if missing)
    config = load_config(args.config)

    # CLI flags override config
    if args.urls:
        config["urls"] = args.urls
    if args.interval is not None:
        config["interval"] = args.interval
    if args.timeout is not None:
        config["timeout"] = args.timeout
    if args.max_retries is not None:
        config["max_retries"] = args.max_retries

    # Interactive fallback: ask for URL if none provided
    if not config["urls"]:
        url = input("Enter the URL to monitor (include https://): ").strip()
        if not url:
            logger.error("No URL provided. Exiting.")
            sys.exit(1)
        if not url.startswith(("http://", "https://")):
            logger.error("URL must start with http:// or https://")
            sys.exit(1)
        config["urls"] = [url]

    # Validate URLs
    for url in config["urls"]:
        if not url.startswith(("http://", "https://")):
            logger.error(f"Invalid URL: {url} — must start with http:// or https://")
            sys.exit(1)

    monitor = Monitor(config, logger)
    monitor.run()


if __name__ == "__main__":
    main()
