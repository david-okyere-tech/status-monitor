# 🟢 Status Monitor

A Python website uptime monitor with response time tracking, email alerting, and summary reports.

## Features

- **argparse CLI** — no `input()` prompts, everything via flags
- **Multi-URL monitoring** — watch as many sites as you want
- **Proper URL validation** — uses `urllib.parse.urlparse` (not just string checks)
- **Multiple expected status codes** — `--expected-status 200 --expected-status 201`
- **Response time tracking** — latency in ms for every check, in console + logs
- **Retry logic** — retries before declaring DOWN (avoids false alarms)
- **Consecutive failure threshold** — `--alert-threshold 3` only alerts after 3 DOWNs in a row
- **Email alerts** — optional `--email` flag, sends on DOWN events via Gmail SMTP
- **Persistent date-stamped logs** — `logs/status_log_2026-06-04.txt`, append mode, never overwritten
- **JSONL history** — every check appended as one line to `logs/history.jsonl` (fast, no read-rewrite)
- **Summary report** — after completion: uptime %, avg/min/max response time, downtime periods
- **Graceful Ctrl+C** — stops cleanly, still prints summary and closes files
- **Configurable log directory** — `--log-dir ./my-logs`
- **Python 3.8+ compatible** — `from __future__ import annotations`

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Monitor a single URL (runs until Ctrl+C)
python status_monitor.py --url https://example.com

# Run 10 checks, every 30 seconds
python status_monitor.py --url https://example.com --interval 30 --checks 10

# Monitor multiple URLs
python status_monitor.py --url https://example.com --url https://google.com

# Accept multiple status codes as "UP"
python status_monitor.py --url https://example.com --expected-status 200 --expected-status 201

# Only alert after 3 consecutive DOWN checks
python status_monitor.py --url https://example.com --alert-threshold 3

# Custom log directory
python status_monitor.py --url https://example.com --log-dir ./my-logs
```

## CLI Options

| Flag | Default | Description |
|------|---------|-------------|
| `--url` | *(required)* | URL to monitor (use multiple times for several sites) |
| `--interval` | 60 | Seconds between checks |
| `--checks` | 0 | Number of checks (0 = run until Ctrl+C) |
| `--timeout` | 10 | HTTP request timeout in seconds |
| `--retries` | 2 | Retries before declaring DOWN |
| `--expected-status` | 200 | Expected HTTP status code(s) — use multiple times |
| `--alert-threshold` | 1 | Consecutive DOWN checks before sending alert/email |
| `--email` | *(none)* | Email address for DOWN alerts |
| `--log-dir` | ./logs | Directory for log and history files |
| `-v, --verbose` | off | Verbose output |

## Email Alerts

When a site goes DOWN, optionally get an email alert:

```bash
# Set Gmail SMTP credentials as environment variables
export SMTP_USER="yourbot@gmail.com"
export SMTP_PASS="your-16-char-app-password"

# Run with email alerts
python status_monitor.py --url https://example.com --email you@gmail.com
```

### Gmail Setup

You **must** use an App Password, not your regular Gmail password:

1. Go to [Google App Passwords](https://myaccount.google.com/apppasswords)
2. Sign in, select "Mail" and your device, click **Generate**
3. Use the 16-character password as `SMTP_PASS`

```bash
export SMTP_USER="yourbot@gmail.com"
export SMTP_PASS="abcdefghijklmnop"   # 16-char app password (spaces optional)
```

## Alert Threshold

The `--alert-threshold` flag controls how many consecutive DOWN checks must occur before an alert is triggered. This prevents spam from brief blips:

```bash
# Alert immediately on first DOWN (default)
python status_monitor.py --url https://example.com --email you@gmail.com

# Only alert after 3 consecutive DOWN checks
python status_monitor.py --url https://example.com --email you@gmail.com --alert-threshold 3
```

When below the threshold, DOWN checks still show ⬇️ in the console. Once the threshold is hit, the icon switches to 🚨 and the email fires.

## Output

### Console

```
🔍 Monitoring 1 URL(s) every 30s
   Running 10 check(s)
   📧 Email alerts → you@gmail.com (threshold: 3)
   📄 Log file: /path/to/logs/status_log_2026-06-04.txt
   📊 History: /path/to/logs/history.jsonl

✅ [2026-06-04 04:52:00] https://example.com: UP (200) 142ms
⚠️  [2026-06-04 04:52:30] https://example.com: WARNING (503) 89ms - Expected 200, got 503
⬇️  [2026-06-04 04:53:00] https://example.com: DOWN - Connection failed
⬇️  [2026-06-04 04:53:30] https://example.com: DOWN - Connection failed
🚨 [2026-06-04 04:54:00] https://example.com: DOWN - Connection failed
  📧 Alert email sent to you@gmail.com

============================================================
  MONITORING SUMMARY
============================================================

  📍 https://example.com
  ──────────────────────────────────────────────────
  Total checks:     10
  UP: 7  |  WARNING: 1  |  DOWN: 2
  Uptime (UP+WARN): 80.0%
  Response time:    avg 135ms  |  min 89ms  |  max 201ms
  Downtime periods:
    • 2026-06-04 04:53:00 → 2026-06-04 04:54:00

============================================================
📄 Log saved to: /path/to/logs/status_log_2026-06-04.txt
📊 History saved to: /path/to/logs/history.jsonl
```

### Log File (`logs/status_log_2026-06-04.txt`)

```
============================================================
Status Monitor Session
Started: 2026-06-04 04:52:00
URLs: https://example.com
Interval: 30s | Timeout: 10s | Retries: 2
Expected statuses: 200
Alert threshold: 3 consecutive DOWN(s)
============================================================
[2026-06-04 04:52:00] https://example.com -> UP | Status: 200 | Response: 142ms
[2026-06-04 04:52:30] https://example.com -> WARNING | Status: 503 | Response: 89ms | Error: Expected 200, got 503
[2026-06-04 04:53:00] https://example.com -> DOWN | Error: Connection failed

──────────────────────────────────────────────────────
Session ended: 2026-06-04 04:57:00
Total checks: 10
============================================================
```

### JSONL History (`logs/history.jsonl`)

Each line is a self-contained JSON object — fast to append, easy to parse:

```jsonl
{"url": "https://example.com", "timestamp": "2026-06-04 04:52:00", "status": "UP", "status_code": 200, "response_time_ms": 142.3, "error": null}
{"url": "https://example.com", "timestamp": "2026-06-04 04:52:30", "status": "WARNING", "status_code": 503, "response_time_ms": 89.1, "error": "Expected 200, got 503"}
```

## Tech Stack

- Python 3.8+
- [requests](https://docs.python-requests.org/) — HTTP client
- Standard library: `argparse`, `urllib.parse`, `smtplib`, `json`, `email`

## License

[MIT](LICENSE)
