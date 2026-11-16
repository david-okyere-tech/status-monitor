import time

from status_monitor.checks import make_result
from status_monitor.history import HistoryStore
from status_monitor.lock import acquire_lock, release_lock
from status_monitor.models import TargetConfig
from status_monitor.stats import URLStats, percentile, sparkline, uptime_percent
from status_monitor.web import Hub


def _result(url, status, stamp, ts, **kwargs):
    target = TargetConfig(url=url, name=kwargs.pop("name", url))
    result = make_result(target, status, kwargs.get("code"), kwargs.get("ms"), kwargs.get("error"), 1)
    result.timestamp = stamp
    result.ts_unix = ts
    return result


def test_percentiles_and_uptime():
    assert percentile([], 50) is None
    assert percentile([5], 95) == 5
    assert percentile([1, 2, 3, 4], 50) == 2.5
    assert uptime_percent(0, 0, 0) is None
    assert uptime_percent(1, 1, 2) == 50
    assert sparkline([1, 2, 3])


def test_session_stats_track_downtime_without_growing_forever():
    stats = URLStats()
    target_url = "https://example.com"
    stats.update(_result(target_url, "UP", "t0", 1, code=200, ms=10))
    stats.update(_result(target_url, "WARNING", "t1", 2, code=503, ms=12, error="got 503"))
    stats.update(_result(target_url, "DOWN", "t2", 3, error="Connection failed"))
    stats.update(_result(target_url, "UP", "t3", 4, code=200, ms=8))
    assert stats.total == 4
    assert stats.uptime == 75
    assert stats.downtime_periods == [("t2", "t3")]
    assert round(stats.avg_rt) == round((10 + 12 + 8) / 3)

    stats.in_downtime = True
    stats.downtime_start = "t9"
    stats.finalize("t10")
    stats.finalize("t11")
    assert stats.downtime_periods[-1] == ("t9", "t10")


def test_sqlite_history_incidents_and_retention(tmp_path):
    store = HistoryStore(str(tmp_path))
    try:
        now = time.time()
        url = "https://example.com"
        store.record_results(
            [
                _result(url, "UP", "t0", now - 100, code=200, ms=30, name="Example"),
                _result(url, "DOWN", "t1", now - 90, error="Connection failed", name="Example"),
                _result(url, "DOWN", "t2", now - 80, error="Connection failed", name="Example"),
                _result(url, "UP", "t3", now - 70, code=200, ms=40, name="Example"),
            ]
        )
        counts = store.counts(url, None)
        assert counts["UP"] == 2
        assert counts["DOWN"] == 2
        assert store.open_incident_count() == 0
        incidents = store.incidents()
        assert incidents[0]["ended_at"] == "t3"
        assert incidents[0]["error"] == "Connection failed"
        recent = store.recent(url, 10)
        assert [item["status"] for item in recent] == ["UP", "DOWN", "DOWN", "UP"]
        assert sorted(store.latencies(url, None)) == [30.0, 40.0]

        store.record_results([_result(url, "DOWN", "old", now - 10 * 86400, error="old", name="Example")])
        assert store.open_incident_count() == 1
        store.prune(now - 86400)
        assert store.counts(url, None)["DOWN"] == 2
        # The open incident started outside the window and is kept.
        assert store.open_incident_count() == 1
        lines = (tmp_path / "history.jsonl").read_text().strip().splitlines()
        assert len(lines) == 5
        assert '"v": 5' in lines[0]
    finally:
        store.close()


def test_lock_is_exclusive(tmp_path):
    first = acquire_lock(str(tmp_path))
    assert first is not None
    assert acquire_lock(str(tmp_path)) is None
    release_lock(first)
    second = acquire_lock(str(tmp_path))
    assert second is not None
    release_lock(second)


def test_health_tracks_a_stale_loop():
    hub = Hub(interval=10)
    code, body = hub.health()
    assert code == 200
    assert body["ready"] is False
    hub.publish({"cycle": 1})
    code, body = hub.health()
    assert code == 200
    assert body["ready"] is True
    hub.published_unix = time.time() - 120
    code, body = hub.health()
    assert code == 503
    assert body["status"] == "stale"
