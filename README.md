# Website Status Monitor

A Python tool that monitors website uptime by sending HTTP requests at regular intervals and logging the results.

## What It Does
- Accepts a target URL from user input
- Sends HTTP GET requests at set intervals
- Detects uptime, downtime, timeouts, and connection errors
- Logs all check results with timestamps to a file
- Handles invalid URLs and network failures gracefully

## Tech Stack
- Python 3
- `requests` library
- `datetime` for timestamps
- File I/O for persistent logging

## How to Run
1. Clone the repo
2. Install dependencies: `pip install requests`
3. Run: `python status_monitor.py`
4. Enter a URL (e.g., `https://google.com`)

## Sample Output
