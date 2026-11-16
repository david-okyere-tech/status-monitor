"""Rich live dashboard for an interactive terminal."""

from __future__ import annotations

from rich import box
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from status_monitor.stats import fmt_ms, fmt_pct, sparkline

_STATUS_STYLE = {
    "UP": "bold green",
    "WARNING": "bold yellow",
    "DOWN": "bold red",
    "PENDING": "dim",
}
_HEADLINE_STYLE = {
    "operational": "bold green",
    "degraded": "bold yellow",
    "outage": "bold red",
    "major": "bold red",
    "pending": "bold cyan",
}


def render_dashboard(payload: dict, *, remaining: float, notices: list[str] | None = None):
    summary = payload["summary"]
    uptime = fmt_pct(summary.get("uptime_24h"))
    median = fmt_ms(summary.get("latency_p50"))
    header = Text()
    header.append(payload["headline"] + "\n", style=_HEADLINE_STYLE.get(payload["overall"], "bold"))
    header.append(
        (
            f"{summary['monitors']} monitors"
            f"  ·  {summary['up']} up"
            f"  ·  {summary['warning']} degraded"
            f"  ·  {summary['down']} down"
            f"  ·  24h {uptime}"
            f"  ·  p50 {median}"
            f"  ·  next check {max(0, remaining):.0f}s"
            f"  ·  cycle {payload.get('cycle', 0)}"
        ),
        style="dim",
    )

    table = Table(box=box.SIMPLE_HEAD, expand=True, header_style="bold", padding=(0, 1))
    table.add_column("Monitor", width=18, no_wrap=True, overflow="ellipsis")
    table.add_column("Status", width=9, no_wrap=True)
    table.add_column("Latency", width=8, justify="right", no_wrap=True)
    table.add_column("Trend", width=12, no_wrap=True)
    table.add_column("Uptime", width=6, justify="right", no_wrap=True)
    table.add_column("TLS", width=5, justify="right", no_wrap=True)
    table.add_column("Detail", ratio=1, no_wrap=True, overflow="ellipsis")

    for group in payload.get("groups", []):
        table.add_row(Text(group["name"].upper(), style="bold cyan"))
        for target in group["targets"]:
            label = Text(target["name"], style="bold")
            status = target["status"]
            samples = [item["ms"] for item in target.get("recent", []) if item.get("ms") is not None]
            tls = "—"
            if target.get("ssl_days_remaining") is not None:
                tls = f"{target['ssl_days_remaining']}d"
            detail = target.get("error") or "—"
            table.add_row(
                label,
                Text(status, style=_STATUS_STYLE.get(status, "")),
                fmt_ms(target.get("response_time_ms")),
                sparkline(samples, width=12),
                fmt_pct(target.get("uptime_pct"), digits=0 if (target.get("uptime_pct") or 0) >= 99.5 else 1),
                tls,
                detail,
            )

    blocks = [
        Panel(header, title="Status Monitor", subtitle=f"v{payload.get('version', '')}", border_style="bright_black"),
        table,
    ]
    incidents = payload.get("incidents") or []
    if incidents:
        lines = Text()
        for incident in incidents[:5]:
            if incident.get("ended_at"):
                lines.append("resolved  ", style="green")
                lines.append(f"{incident['name']}  {incident['started_at']} → {incident['ended_at']}\n")
            else:
                lines.append("open      ", style="bold red")
                detail = incident.get("error") or ""
                lines.append(f"{incident['name']}  since {incident['started_at']}  {detail}\n")
        blocks.append(Panel(lines, title="Incidents", border_style="bright_black"))
    if notices:
        note = Text("\n".join(notices[-4:]), style="dim")
        blocks.append(Panel(note, title="Alerts", border_style="bright_black"))
    return Group(*blocks)
