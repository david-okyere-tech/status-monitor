# 🟢 Status Monitor

A Python website uptime monitor — async I/O, persistent crash-safe alert state, exponential backoff, and rotating logs.

## Features

**Monitoring**
- **Async URL checking** (`httpx.AsyncClient`) — all URLs checked concurrently each cycle; one slow site won't block the others
- **Exponential backoff with jitter** on retries, both for connection failures and unexpected status codes
- **Configurable SSL** — per-target `verify_ssl` for internal services with self-signed certs
- **Interval drift correction** — sleeps `interval - elapsed`, so checks stay on schedule even when requests are slow

**Alerting**
- **Persistent alert state** (`.state.json`) — survives crashes/restarts, so you don't lose track of an in-progress outage or fire duplicate alerts on restart
- **Alert once per outage**, not on every check
- **Recovery notifications** — email when a site comes back UP, including outage start time
- **Cooldown on flapping** — suppresses repeat alerts for a few checks after recovery
- **WARNING ≠ recovery** — an unexpected-but-reachable status code doesn't end an outage; only UP does
- **Configurable SMTP** — Gmail, SendGrid, SES, Mailgun, or any SMTP server via `aiosmtplib`

**Data & Reporting**
- **JSONL history** — append-only, schema-versioned (`logs/history.jsonl`)
- **Rotating logs** — `logs/monitor.log`, capped at 5MB × 3 backups, so long-running processes don't fill the disk
- **O(1)-ish summary stats** — running totals/response-time stats per URL rather than storing every result; downtime periods are capped so long-running monitors stay bounded
- **Summary report** on shutdown — uptime %, response time stats, downtime periods per URL

**Process Management**
- **OS-level file locking** (`fcntl.flock`) — refuses to start a second instance against the same log directory
- **systemd unit** — run as a hardened service that restarts on crash

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Monitor a single URL (runs until Ctrl+C)
python status_monitor.py --url https://example.com

# Monitor multiple URLs (checked concurrently)
python status_monitor.py --url https://example.com --url https://google.com

# With email alerts
python status_monitor.py --url https://example.com --email you@example.com

# From a config file
python status_monitor.py --config config.json
```

## Config File

`--config` loads targets and global defaults from a JSON file. It combines with `--url`, not
replaces it — targets from `--config` and any repeated `--url` flags are both monitored together:

```json
{
  "global": {
    "expected_statuses": [200],
    "timeout": 10,
    "retries": 2,
    "verify_ssl": true,
    "alert_threshold": 1
  },
  "targets": [
    { "url": "https://example.com" },
    { "url": "https://internal.corp.local", "verify_ssl": false, "alert_threshold": 3 }
  ]
}
```

Any field set at the target level overrides the `global` default for that target.

## CLI Options

Verified directly against the `argparse` block in `main()` — there is no `--checks`,
`--strict-uptime`, or `-v`; those were never implemented in v4.

| Flag | Default | Description |
|------|---------|-------------|
| `--config` | *(none)* | Path to a JSON config file (see above) |
| `--url` | *(required if no `--config`)* | URL to monitor (repeatable; combines with `--config`) |
| `--interval` | 60 | Seconds between check cycles |
| `--timeout` | 10 | HTTP request timeout in seconds |
| `--retries` | 2 | Retries after the first attempt |
| `--expected-status` | 200 | Expected HTTP status code (repeatable) — applies to all `--url` targets |
| `--alert-threshold` | 1 | Consecutive DOWN checks before an alert fires |
| `--email` | *(none)* | Email address for DOWN/recovery alerts |
| `--log-dir` | ./logs | Directory for lock file, state file, logs, and history |
| `--no-verify-ssl` | off | Disable SSL certificate verification |

## Email Alerts

Set SMTP credentials as environment variables (or use `.env` with the systemd `EnvironmentFile` setup below):

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

python status_monitor.py --url https://example.com --email you@example.com
```

### Gmail Setup

You **must** use an App Password, not your regular Gmail password:

1. Go to [Google App Passwords](https://myaccount.google.com/apppasswords)
2. Sign in, select "Mail" and your device, click **Generate**
3. Use the 16-character password as `SMTP_PASS`

## Alert Behavior

1. **DOWN detected** — per-URL counter increments
2. **Threshold crossed** — alert fires once, not on every check
3. **Site recovers (UP)** — recovery email sent with outage start time; cooldown begins
4. **Cooldown** — DOWN checks during the cooldown window don't fire alerts (prevents flap spam). The
   cooldown counter keeps counting down through those suppressed DOWN checks — it does not reset — so
   once it hits 0 the very next DOWN check can alert immediately. If the site stays up instead, the
   cooldown is cleared, so a later, unrelated outage isn't wrongly suppressed by a stale leftover
   cooldown from an old flap.
5. **WARNING ≠ recovery** — a WARNING does not end an outage; only UP does
6. **Crash-safe** — alert state is persisted to `.state.json`, so a restart mid-outage won't re-fire or lose track of an already-alerted outage. If `.state.json` is ever corrupted, it's quarantined to `.state.json.corrupt`, a warning is logged, and monitoring starts with fresh alert state rather than failing silently.

```
⬇️  Check 1: DOWN (below threshold, no alert)
⬇️  Check 2: DOWN (below threshold, no alert)
🚨  Check 3: DOWN (threshold reached, alert fires 📧)
⬇️  Check 4: DOWN (already alerted, no duplicate)
⚠️  Check 5: WARNING 500 (site reachable but degraded — outage continues)
⬇️  Check 6: DOWN (still in outage)
✅  Check 7: UP (recovery email 📧, cooldown starts)
⬇️  Check 8: DOWN (in cooldown — suppressed)
⬇️  Check 9: DOWN (cooldown expired, counter restarts)
```

## Running as a Service

```bash
sudo cp status-monitor.service /etc/systemd/system/

# EDIT THE URL(s) in the service file before enabling!
sudo nano /etc/systemd/system/status-monitor.service

cp .env.example /opt/status-monitor/.env
nano /opt/status-monitor/.env

sudo systemctl daemon-reload
sudo systemctl enable status-monitor
sudo systemctl start status-monitor

sudo systemctl status status-monitor
journalctl -u status-monitor -f
```

The service file uses `EnvironmentFile` for SMTP credentials (not inline `Environment=`) because
`systemctl show` reveals inline values to any user with access to systemd. It also sets
`ProtectSystem=strict`, `NoNewPrivileges=true`, and `PrivateTmp=true` for basic sandboxing —
note that `ReadWritePaths` must match wherever `--log-dir` actually points, or writes will fail
silently under the sandbox.

## Output

### Console

```
🔍 Monitoring 2 URL(s) every 60s (Async)
   📧 Alerts → you@example.com
   🔒 Lock: Active | 📊 History: /path/to/logs/history.jsonl

✅ [2026-06-04 04:52:00] https://example.com: UP (200)
✅ [2026-06-04 04:52:00] https://google.com: UP (200)
⚠️  [2026-06-04 04:53:00] https://example.com: WARNING (503) Expected [200], got 503
🚨 [2026-06-04 04:55:00] https://example.com: DOWN Connection failed
  📧 Alert email sent to you@example.com
✅ [2026-06-04 04:56:00] https://example.com: UP (200)
  📧 Recovery email sent to you@example.com

============================================================
  MONITORING SUMMARY
============================================================

  📍 https://example.com
  Total: 5 | UP: 3 | WARN: 1 | DOWN: 1 | Uptime: 80.0%
  Response: avg 110ms | min 89ms | max 142ms
```

### JSONL History (`logs/history.jsonl`)

```jsonl
{"v": 4, "url": "https://example.com", "timestamp": "2026-06-04 04:52:00", "status": "UP", "status_code": 200, "response_time_ms": 142.3, "error": null, "attempt": 1}
{"v": 4, "url": "https://example.com", "timestamp": "2026-06-04 04:54:00", "status": "DOWN", "status_code": null, "response_time_ms": null, "error": "Connection failed", "attempt": 3}
```

## Tech Stack

- Python 3.10+ (`from __future__ import annotations`, `asyncio`)
- [httpx](https://www.python-httpx.org/) — async HTTP client
- [aiosmtplib](https://github.com/cole/aiosmtplib) — async SMTP for email alerts
- Standard library: `asyncio`, `argparse`, `fcntl`, `logging.handlers`, `json`, `dataclasses`

## License

[MIT](LICENSE)
