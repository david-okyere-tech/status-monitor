"""Check loop: run targets, persist history, alert, refresh the dashboards."""

from __future__ import annotations

import asyncio
import os
import signal
import time
from datetime import datetime
from typing import Optional

import httpx

from status_monitor.alerts import AlertStateManager, Notifier
from status_monitor.checks import check_target
from status_monitor.history import HistoryStore
from status_monitor.lock import acquire_lock, lock_path, release_lock
from status_monitor.logging_setup import setup_logging
from status_monitor.models import STATE_FILENAME, CheckResult, Settings, TargetConfig
from status_monitor.snapshot import build_payload
from status_monitor.stats import URLStats, percentile
from status_monitor.web import Hub, start_web

_PRUNE_EVERY = 30


async def run_loop(settings: Settings, notifier: Optional[Notifier] = None) -> int:
    os.makedirs(settings.log_dir, exist_ok=True)
    lock = acquire_lock(settings.log_dir)
    if lock is None:
        print(
            "❌ Another instance is already running "
            f"(Lockfile: {lock_path(settings.log_dir)})"
        )
        return 1

    logger = setup_logging(settings.log_dir, verbose=settings.verbose)
    history: Optional[HistoryStore] = None
    server = None
    own_notifier = notifier is None
    notices: list[str] = []
    stop = asyncio.Event()
    code = 0
    ran = False

    def output(message: str) -> None:
        logger.info(message)
        notices.append(message.strip())
        del notices[:-8]
        if not settings.dashboard and not settings.quiet:
            print(message)

    if own_notifier:
        notifier = Notifier(settings.email, settings.webhooks, output=output)

    try:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except (NotImplementedError, RuntimeError, ValueError):
                pass

        history = HistoryStore(settings.log_dir)
        state_mgr = AlertStateManager(os.path.join(settings.log_dir, STATE_FILENAME))
        stats = {target.url: URLStats() for target in settings.targets}
        thresholds = {target.url: target.alert_threshold for target in settings.targets}
        hub = Hub(settings.interval)
        latest: dict[str, CheckResult] = {}
        session_started = time.time()
        cycle = 0

        hub.seed(
            build_payload(
                settings.targets,
                history,
                latest,
                interval=settings.interval,
                cycle=0,
            )
        )
        if settings.web:
            try:
                server = start_web(hub, settings.web_host, settings.web_port)
            except OSError as exc:
                print(f"❌ Could not bind status page on {settings.web_host}:{settings.web_port}: {exc}")
                code = 1
                return code
            bound_host, bound_port = server.server_address[:2]
            page = f"http://{bound_host}:{bound_port}"
            print(f"🌐 Status page: {page}")
            logger.info("Status page: %s", page)

        if not settings.dashboard and not settings.quiet:
            _print_banner(settings, history.jsonl_path)

        ran = True
        limits = httpx.Limits(max_connections=max(20, settings.concurrency), max_keepalive_connections=20)
        async with httpx.AsyncClient(verify=True, limits=limits) as verified, httpx.AsyncClient(
            verify=False, limits=limits
        ) as unverified:
            semaphore = asyncio.Semaphore(max(1, settings.concurrency))

            async def run_one(target: TargetConfig) -> CheckResult:
                async with semaphore:
                    return await check_target(
                        target,
                        http_client=verified,
                        unverified_client=unverified,
                    )

            live = _start_dashboard(settings, hub.payload(), notices) if settings.dashboard else None
            try:
                while not stop.is_set():
                    started = time.monotonic()
                    results = await asyncio.gather(*(run_one(target) for target in settings.targets))
                    await _record_cycle(
                        settings,
                        results,
                        stats,
                        state_mgr,
                        history,
                        notifier,
                        thresholds,
                        logger,
                        latest,
                    )
                    cycle += 1
                    if settings.retention_days > 0 and cycle % _PRUNE_EVERY == 0:
                        cutoff = time.time() - settings.retention_days * 86400
                        try:
                            await asyncio.to_thread(history.prune, cutoff)
                        except Exception:
                            logger.exception("failed to prune history")
                    payload = build_payload(
                        settings.targets,
                        history,
                        latest,
                        interval=settings.interval,
                        cycle=cycle,
                    )
                    hub.publish(payload)
                    if live is not None:
                        live.update(_dashboard(payload, remaining=0, notices=notices))
                    if settings.once or stop.is_set():
                        break
                    delay = max(0.0, settings.interval - (time.monotonic() - started))

                    def tick(remaining: float, snapshot=payload) -> None:
                        if live is not None:
                            live.update(_dashboard(snapshot, remaining=remaining, notices=notices))

                    if await _wait(delay, stop, tick if live is not None else None):
                        break
            finally:
                if live is not None:
                    live.stop()

        if stop.is_set() and not settings.once:
            print("\n👋 Stopped.")
        _print_summary(stats, history, session_started)
    except asyncio.CancelledError:
        if not settings.once:
            print("\n👋 Stopped.")
        if ran and history is not None:
            _print_summary(stats, history, session_started)
        raise
    finally:
        if notifier is not None and own_notifier:
            await notifier.aclose()
        if server is not None:
            server.shutdown()
            server.server_close()
        if history is not None:
            history.close()
        release_lock(lock)
    return code


async def _record_cycle(
    settings: Settings,
    results: list[CheckResult],
    stats: dict[str, URLStats],
    state_mgr: AlertStateManager,
    history: HistoryStore,
    notifier: Optional[Notifier],
    thresholds: dict[str, int],
    logger,
    latest: dict[str, CheckResult],
) -> None:
    for result in results:
        latest[result.url] = result
        stats.setdefault(result.url, URLStats()).update(result)
        should_alert, is_recovery, outage_start = state_mgr.update(
            result,
            thresholds.get(result.url, 1),
            cooldown_checks=settings.cooldown_checks,
            persist=False,
        )
        line = result.line()
        logger.info(line)
        if not settings.dashboard and not settings.quiet:
            print(line)
        if notifier is not None and (should_alert or is_recovery):
            event = "down" if should_alert else "recovery"
            await notifier.dispatch(event, result, outage_start)

    try:
        await asyncio.to_thread(state_mgr.save)
    except Exception:
        logger.exception("failed to save alert state")
    try:
        await asyncio.to_thread(history.record_results, results)
    except Exception:
        logger.exception("failed to write history")


def _print_banner(settings: Settings, history_path: str) -> None:
    interval = int(settings.interval) if settings.interval == int(settings.interval) else settings.interval
    print(f"🔍 Monitoring {len(settings.targets)} URL(s) every {interval}s (Async)")
    if settings.email:
        print(f"   📧 Alerts → {settings.email}")
    if settings.webhooks:
        print(f"   📨 Webhooks: {len(settings.webhooks)}")
    print(f"   🔒 Lock: Active | 📊 History: {history_path}\n")


def _print_summary(stats: dict[str, URLStats], history: Optional[HistoryStore], session_started: float) -> None:
    print("\n" + "=" * 60 + "\n  MONITORING SUMMARY\n" + "=" * 60)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for url, stat in stats.items():
        stat.finalize(stamp)
        uptime = ((stat.up + stat.warning) / stat.total * 100) if stat.total else 0
        print(
            f"\n  📍 {url}\n"
            f"  Total: {stat.total} | UP: {stat.up} | WARN: {stat.warning} | "
            f"DOWN: {stat.down} | Uptime: {uptime:.1f}%"
        )
        if stat.rt_count:
            print(f"  Response: avg {stat.avg_rt:.0f}ms | min {stat.rt_min:.0f}ms | max {stat.rt_max:.0f}ms")
            samples: list[float] = []
            if history is not None:
                try:
                    samples = history.latencies(url, session_started, limit=5000)
                except Exception:
                    samples = []
            p50 = percentile(samples, 50)
            p95 = percentile(samples, 95)
            p99 = percentile(samples, 99)
            if p50 is not None and p95 is not None and p99 is not None:
                print(f"  Latency: p50 {p50:.0f}ms | p95 {p95:.0f}ms | p99 {p99:.0f}ms")
        if stat.downtime_periods:
            print("  Downtime:")
            for start, end in stat.downtime_periods[-5:]:
                print(f"    {start} → {end}")


def _start_dashboard(settings: Settings, payload: dict, notices: list[str]):
    from rich.console import Console
    from rich.live import Live

    console = Console()
    live = Live(
        _dashboard(payload, remaining=settings.interval, notices=notices),
        console=console,
        screen=bool(console.is_terminal),
        refresh_per_second=4,
    )
    live.start()
    return live


def _dashboard(payload: dict, *, remaining: float, notices: list[str]):
    from status_monitor.tui import render_dashboard

    return render_dashboard(payload, remaining=remaining, notices=notices)


async def _wait(seconds: float, stop: asyncio.Event, on_tick=None) -> bool:
    """Return True when `stop` is set before the delay elapses."""
    if stop.is_set():
        return True
    if seconds <= 0:
        return False
    deadline = time.monotonic() + seconds
    while True:
        if stop.is_set():
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        if on_tick is not None:
            on_tick(remaining)
        step = min(0.4, remaining) if on_tick is not None else remaining
        try:
            await asyncio.wait_for(stop.wait(), timeout=step)
            return True
        except asyncio.TimeoutError:
            continue
