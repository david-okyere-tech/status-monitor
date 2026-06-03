# 🟢 Status Monitor

A Python website uptime monitor with multi-URL support, configurable intervals, retry logic, and alerting (webhook + email).

## Features

- **Multi-URL monitoring** — watch as many sites as you want
- **Configurable intervals** — check every N seconds, per config or CLI flag
- **Retry logic** — retries before declaring a site DOWN (no false alarms from blips)
- **Response time tracking** — measures and logs latency in milliseconds
- **Alerting**
  - 🖥️ Console with emoji status (✅ / ⚠️ / 🚨)
  - 📬 Email alerts on downtime (SMTP)
  - 🔗 Webhook alerts (Slack, Discord, custom)
- **Persistent history** — JSON log of all checks for analysis
- **Structured logging** — daily log files in `logs/`
- **Config file** — YAML config with CLI override flags
- **Graceful shutdown** — Ctrl+C stops cleanly

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Quick: monitor a single URL
python status_monitor.py -u https://example.com

# Monitor multiple URLs
python status_monitor.py -u https://example.com -u https://google.com

# Custom interval (30 seconds)
python status_monitor.py -u https://example.com --interval 30
```

## Configuration

Copy the example config and edit:

```bash
cp config.example.yaml config.yaml
```

```yaml
urls:
  - https://example.com
  - https://google.com

interval: 60
timeout: 10
max_retries: 2
expected_status: 200

alerts:
  # webhook: https://hooks.slack.com/services/XXX/YYY/ZZZ
  # email:
  #   smtp_host: smtp.gmail.com
  #   smtp_port: 587
  #   sender: bot@example.com
  #   recipient: you@example.com
  #   username: bot@example.com
  #   password: app-password-here
  #   use_tls: true
```

Then run with the config:

```bash
python status_monitor.py --config config.yaml
```

## CLI Options

| Flag | Description |
|------|-------------|
| `-u, --url` | URL to monitor (repeat for multiple) |
| `-c, --config` | Config file path (default: `config.yaml`) |
| `-i, --interval` | Check interval in seconds |
| `-t, --timeout` | HTTP request timeout |
| `-r, --max-retries` | Retries before declaring DOWN |
| `-v, --verbose` | Debug-level logging |

## Output

### Console
```
2026-06-04 04:52:00 [INFO   ] ✅ [2026-06-04T04:52:00Z] https://example.com: UP (200) 142ms
2026-06-04 04:52:00 [WARNING] ⚠️  [2026-06-04T04:52:00Z] https://api.example.com: WARNING (503) 89ms - Expected 200, got 503
2026-06-04 04:52:00 [WARNING] 🚨 ALERT: [2026-06-04T04:52:00Z] https://down.example.com: DOWN - Connection failed
```

### History (history.json)
```json
[
  {
    "url": "https://example.com",
    "timestamp": "2026-06-04T04:52:00+00:00",
    "status": "UP",
    "status_code": 200,
    "response_time_ms": 142.3,
    "error": null
  }
]
```

## Tech Stack

- Python 3.8+
- [requests](https://docs.python-requests.org/) — HTTP client
- [PyYAML](https://pyyaml.org/) — config parsing
- Standard library: `logging`, `json`, `smtplib`, `argparse`, `signal`

## License

[MIT](LICENSE)
