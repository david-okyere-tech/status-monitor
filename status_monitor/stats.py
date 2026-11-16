"""Session counters and latency percentiles.

Uptime counts a WARNING as reachable: the process answered, even if the
status code or content was not what we wanted. That matches the v4 summary.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from status_monitor.models import MAX_DOWNTIME_PERIODS, CheckResult


def percentile(values: list[float], p: float) -> Optional[float]:
    """Linear-interpolation percentile. `p` is 0–100."""
    if not values:
        return None
    if p < 0 or p > 100:
        raise ValueError("percentile must be between 0 and 100")
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (len(ordered) - 1) * (p / 100.0)
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return float(ordered[int(rank)])
    weight = rank - low
    return float(ordered[low] * (1 - weight) + ordered[high] * weight)


def uptime_percent(up: int, warning: int, down: int) -> Optional[float]:
    total = up + warning + down
    if total <= 0:
        return None
    return (up + warning) / total * 100.0


def fmt_ms(value: Optional[float]) -> str:
    if value is None:
        return "—"
    if value >= 1000:
        return f"{value / 1000:.2f}s"
    return f"{value:.0f}ms"


def fmt_pct(value: Optional[float], digits: int = 1) -> str:
    if value is None:
        return "—"
    return f"{value:.{digits}f}%"


def sparkline(values: list[float], width: int = 24) -> str:
    blocks = "▁▂▃▄▅▆▇█"
    sample = [v for v in values if v is not None][-width:]
    if not sample:
        return "—"
    lo = min(sample)
    hi = max(sample)
    span = hi - lo or 1.0
    chars = []
    for value in sample:
        index = int(round((value - lo) / span * (len(blocks) - 1)))
        chars.append(blocks[index])
    return "".join(chars)


@dataclass
class URLStats:
    """Bounded-memory statistics tracker for the in-process summary."""

    total: int = 0
    up: int = 0
    down: int = 0
    warning: int = 0
    rt_sum: float = 0.0
    rt_min: float = float("inf")
    rt_max: float = 0.0
    rt_count: int = 0
    downtime_periods: list = field(default_factory=list)
    in_downtime: bool = False
    downtime_start: Optional[str] = None

    def update(self, result: CheckResult) -> None:
        self.total += 1
        if result.status == "UP":
            self.up += 1
        elif result.status == "DOWN":
            self.down += 1
        else:
            self.warning += 1

        if result.response_time_ms is not None:
            self.rt_sum += result.response_time_ms
            self.rt_count += 1
            if result.response_time_ms < self.rt_min:
                self.rt_min = result.response_time_ms
            if result.response_time_ms > self.rt_max:
                self.rt_max = result.response_time_ms

        if result.status == "DOWN" and not self.in_downtime:
            self.in_downtime = True
            self.downtime_start = result.timestamp
        elif result.status != "DOWN" and self.in_downtime:
            self.in_downtime = False
            self.downtime_periods.append((self.downtime_start, result.timestamp))
            if len(self.downtime_periods) > MAX_DOWNTIME_PERIODS:
                self.downtime_periods = self.downtime_periods[-MAX_DOWNTIME_PERIODS:]

    def finalize(self, last_timestamp: str) -> None:
        if self.in_downtime and self.downtime_start:
            self.downtime_periods.append((self.downtime_start, last_timestamp))
            if len(self.downtime_periods) > MAX_DOWNTIME_PERIODS:
                self.downtime_periods = self.downtime_periods[-MAX_DOWNTIME_PERIODS:]
            self.in_downtime = False

    @property
    def uptime(self) -> float:
        value = uptime_percent(self.up, self.warning, self.down)
        return 0.0 if value is None else value

    @property
    def avg_rt(self) -> float:
        if not self.rt_count:
            return 0.0
        return self.rt_sum / self.rt_count
