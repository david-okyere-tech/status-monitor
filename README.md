# 🟢 Status Monitor

A Python website uptime monitor built for real-world use — parallel checking, proper alert lifecycle, configurable SMTP, and process management.

## Features

**Monitoring**
- **Parallel URL checking** — `ThreadPoolExecutor` checks all URLs concurrently; one slow site won't block the others
- **Accurate intervals** — Tracks wall-clock time so checks stay on schedule even when requests take time
- **Consistent retries** — Both connection failures AND unexpected status codes (503, etc.) get retried
- **Configurable SSL** — `--no-verify-ssl` for internal services with self-signed certs
- **Bounded memory** — Results list capped at 10,000 entries for long-running processes

**Alerting**
- **Alert once per outage** — Fires when threshold is crossed, not on every check
- **Recovery notifications** — Email when a site comes back UP, including outage duration
- **Cooldown on flapping** — Prevents spam when a site bounces up/down rapidly
- **WARNING ≠ recovery** — A 500 after a DOWN doesn't trigger "recovered" (only UP does)
- **Configurable SMTP** — Works with Gmail, SendGrid, SES, Mailgun, or any SMTP server

**Data & Reporting**
- **JSONL history** — Append-only, schema-versioned, no read-rewrite bottleneck
- **Date-stamped logs** — `logs/status_log_2026-06-04.txt`, append mode, never overwritten
- **Summary report** — Uptime %, response time stats, downtime periods
- **`--strict-uptime`** — Choose whether WARNING counts as "up" in the summary

**Process Management**
- **PID file** — Detects if another instance using the same log directory is already running
- **Graceful shutdown** — Ctrl+C still prints summary and closes files cleanly
- **systemd unit** — Run as a hardened system service that restarts on crash

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Monitor a single URL (runs until Ctrl+C)
python status_monitor.py --url https://example.com

# Run 10 checks, every 30 seconds
python status_monitor.py --url https://example.com --interval 30 --checks 10

# Monitor multiple URLs (checked in parallel)
python status_monitor.py --url https://example.com --url https://google.com

# Accept multiple status codes as "UP"
python status_monitor.py --url https://example.com --expected-status 200 --expected-status 201

# Only alert after 3 consecutive DOWN checks
python status_monitor.py --url https://example.com --alert-threshold 3

# Self-signed certs on internal services
python status_monitor.py --url https://internal.corp.local --no-verify-ssl
```

## CLI Options

| Flag | Default | Description |
|------|---------|-------------|
| `--url` | *(required)* | URL to monitor (use multiple times for several sites) |
| `--interval` | 60 | Seconds between checks |
| `--checks` | 0 | Number of checks (0 = run until Ctrl+C) |
| `--timeout` | 10 | HTTP request timeout in seconds |
| `--retries` | 2 | Retries AFTER the first attempt (total: 3) |
| `--expected-status` | 200 | Expected HTTP status code(s) — use multiple times |
| `--alert-threshold` | 1 | Consecutive DOWN checks before sending alert |
| `--email` | *(none)* | Email address for DOWN/recovery alerts |
| `--log-dir` | ./logs | Directory for log and history files |
| `--no-verify-ssl` | off | Disable SSL certificate verification |
| `--strict-uptime` | off | Only count UP (not WARNING) in uptime percentage |
| `-v, --verbose` | off | Verbose output |

## Email Alerts

Set SMTP credentials as environment variables:

```bash
# Gmail (default)
export SMTP_USER="yourbot@gmail.com"
export SMTP_PASS="your-16-char-app-password"

# Custom SMTP server (SendGrid, SES, Mailgun, etc.)
export SMTP_HOST="smtp.sendgrid.net"
export SMTP_PORT="587"
export SMTP_USER="apikey"
export SMTP_PASS="your-sendgrid-api-key"
export SMTP_TLS="true"

# Run with alerts
python status_monitor.py --url https://example.com --email you@example.com
```

### Gmail Setup

You **must** use an App Password, not your regular Gmail password:

1. Go to [Google App Passwords](https://myaccount.google.com/apppasswords)
2. Sign in, select "Mail" and your device, click **Generate**
3. Use the 16-character password as `SMTP_PASS`

## Alert Behavior

The alert system handles the full outage lifecycle:

1. **DOWN detected** — Counter increments per URL
2. **Threshold crossed** — Alert fires once (not on every check)
3. **Site recovers (UP)** — Recovery email sent with outage start time and duration
4. **Cooldown** — If site flaps (up/down rapidly), alerts are suppressed for a few checks after recovery
5. **WARNING ≠ recovery** — A WARNING (unexpected status) does NOT end an outage. Only a UP does.

```
⬇️  Check 1: DOWN (below threshold, no alert)
⬇️  Check 2: DOWN (below threshold, no alert)
🚨  Check 3: DOWN (threshold reached, alert fires 📧)
⬇️  Check 4: DOWN (already alerted, no duplicate)
⚠️  Check 5: WARNING 500 (site reachable but degraded — no recovery)
⬇️  Check 6: DOWN (cooldown active from prior alert, suppressed)
✅  Check 7: UP (recovery email 📧, cooldown starts)
⬇️  Check 8: DOWN (in cooldown, suppressed)
⬇️  Check 9: DOWN (cooldown expired, counter restarts)
```

## Running as a Service

```bash
# Copy the service file
sudo cp status-monitor.service /etc/systemd/system/

# EDIT THE URL in the service file before enabling!
sudo nano /etc/systemd/system/status-monitor.service

# Copy and fill in SMTP credentials
cp .env.example /opt/status-monitor/.env
nano /opt/status-monitor/.env

# Enable and start
sudo systemctl daemon-reload
sudo systemctl enable status-monitor
sudo systemctl start status-monitor

# Check status
sudo systemctl status status-monitor

# View logs
journalctl -u status-monitor -f
```

The service file uses `EnvironmentFile` for credentials (not inline `Environment=`)
because `systemctl show` reveals inline values to any user. It also enables
`ProtectSystem=strict`, `NoNewPrivileges=true`, and `PrivateTmp=true` for
basic sandboxing.

## Output

### Console

```
🔍 Monitoring 2 URL(s) every 60s
   Running until Ctrl+C
   📧 Alerts → you@example.com (threshold: 3)
   📄 Log: /path/to/logs/status_log_2026-06-04.txt
   📊 History: /path/to/logs/history.jsonl
   🔑 PID: 12345

✅ [2026-06-04 04:52:00] https://example.com: UP (200) 142ms
✅ [2026-06-04 04:52:00] https://google.com: UP (200) 87ms
⚠️  [2026-06-04 04:53:00] https://example.com: WARNING (503) 89ms - Expected 200, got 503
⬇️  [2026-06-04 04:54:00] https://example.com: DOWN - Connection failed
🚨 [2026-06-04 04:55:00] https://example.com: DOWN - Connection failed
  📧 Alert email sent to you@example.com
✅ [2026-06-04 04:56:00] https://example.com: UP (200) 98ms
  📧 Recovery email sent to you@example.com

============================================================
  MONITORING SUMMARY
============================================================

  📍 https://example.com
  ──────────────────────────────────────────────────
  Total checks:     5
  UP: 3  |  WARNING: 1  |  DOWN: 1
  Uptime (UP+WARN): 80.0%
  Response time:    avg 110ms  |  min 89ms  |  max 142ms
  Downtime periods:
    • 2026-06-04 04:54:00 → 2026-06-04 04:55:00

============================================================
```

### JSONL History (`logs/history.jsonl`)

Each line is schema-versioned and self-contained:

```jsonl
{"v": 3, "url": "https://example.com", "timestamp": "2026-06-04 04:52:00", "status": "UP", "status_code": 200, "response_time_ms": 142.3, "error": null, "attempt": 1}
{"v": 3, "url": "https://example.com", "timestamp": "2026-06-04 04:54:00", "status": "DOWN", "status_code": null, "response_time_ms": null, "error": "Connection failed", "attempt": 3}
```

## Tech Stack

- Python 3.8+ (`from __future__ import annotations`)
- [requests](https://docs.python-requests.org/) — HTTP client
- Standard library: `concurrent.futures`, `argparse`, `urllib.parse`, `smtplib`, `json`, `email`

## License

[MIT](LICENSE)
