"""The outage state machine is the behavior users already depend on."""

import json

from status_monitor.alerts import (
    AlertStateManager,
    Notifier,
    build_email,
    event_payload,
    slack_body,
    webhook_body,
)
from status_monitor.config import infer_webhook_kind
from status_monitor.models import CheckResult, Webhook


def _result(url, status, stamp, *, code=None, error=None):
    return CheckResult(
        url=url,
        timestamp=stamp,
        status=status,
        status_code=code,
        response_time_ms=20.0 if status != "DOWN" else None,
        error=error,
        attempt=1,
        name="Example",
    )


def test_alerts_once_per_outage_and_warning_is_not_recovery(tmp_path):
    manager = AlertStateManager(str(tmp_path / "state.json"))
    url = "https://example.com"

    assert manager.update(_result(url, "DOWN", "t1", error="Connection failed"), 3, persist=False)[:2] == (False, False)
    assert manager.update(_result(url, "DOWN", "t2", error="Connection failed"), 3, persist=False)[:2] == (False, False)
    alert, recovery, started = manager.update(_result(url, "DOWN", "t3", error="Connection failed"), 3, persist=False)
    assert (alert, recovery, started) == (True, False, "t1")
    # Already alerted: a later DOWN in the same outage stays quiet.
    assert manager.update(_result(url, "DOWN", "t4", error="Connection failed"), 3, persist=False)[:2] == (False, False)

    warning = _result(url, "WARNING", "t5", code=503, error="Expected [200], got 503")
    assert manager.update(warning, 3, persist=False) == (False, False, None)
    assert manager.get(url)["alert_fired"] is True
    assert manager.get(url)["outage_start"] == "t1"
    assert manager.get(url)["consecutive_down"] == 0

    assert manager.update(_result(url, "DOWN", "t6", error="Connection failed"), 3, persist=False)[:2] == (False, False)
    recovery_alert, is_recovery, outage_start = manager.update(
        _result(url, "UP", "t7", code=200), 3, cooldown_checks=2, persist=False
    )
    assert (recovery_alert, is_recovery, outage_start) == (False, True, "t1")
    assert manager.get(url)["outage_start"] is None
    assert manager.get(url)["alert_fired"] is False
    assert manager.get(url)["cooldown_remaining"] == 2


def test_cooldown_suppresses_then_allows_the_next_outage(tmp_path):
    manager = AlertStateManager(str(tmp_path / "state.json"))
    url = "https://example.com"
    manager.update(_result(url, "DOWN", "t1", error="down"), 1, persist=False)
    manager.update(_result(url, "UP", "t2", code=200), 1, cooldown_checks=2, persist=False)

    # Both of these DOWN checks fall inside the cooldown window.
    assert manager.update(_result(url, "DOWN", "t3", error="down"), 1, cooldown_checks=2, persist=False)[0] is False
    assert manager.get(url)["cooldown_remaining"] == 1
    assert manager.update(_result(url, "DOWN", "t4", error="down"), 1, cooldown_checks=2, persist=False)[0] is False
    assert manager.get(url)["cooldown_remaining"] == 0

    alert, _recovery, started = manager.update(
        _result(url, "DOWN", "t5", error="down"), 1, cooldown_checks=2, persist=False
    )
    assert alert is True
    assert started == "t3"


def test_plain_up_clears_a_leftover_cooldown(tmp_path):
    manager = AlertStateManager(str(tmp_path / "state.json"))
    url = "https://example.com"
    manager.update(_result(url, "DOWN", "t1", error="down"), 1, persist=False)
    manager.update(_result(url, "UP", "t2", code=200), 1, cooldown_checks=3, persist=False)
    # Not a recovery — the outage already ended — so the cooldown must not linger.
    manager.update(_result(url, "UP", "t3", code=200), 1, cooldown_checks=3, persist=False)
    assert manager.get(url)["cooldown_remaining"] == 0
    alert, _, _ = manager.update(_result(url, "DOWN", "t4", error="down"), 1, persist=False)
    assert alert is True


def test_flap_below_threshold_does_not_arm_cooldown(tmp_path):
    manager = AlertStateManager(str(tmp_path / "state.json"))
    url = "https://example.com"
    manager.update(_result(url, "DOWN", "t1", error="down"), 3, persist=False)
    manager.update(_result(url, "DOWN", "t2", error="down"), 3, persist=False)
    manager.update(_result(url, "UP", "t3", code=200), 3, cooldown_checks=3, persist=False)
    assert manager.get(url)["cooldown_remaining"] == 0
    assert manager.get(url)["alert_fired"] is False


def test_state_round_trip_and_corrupt_file(tmp_path):
    path = tmp_path / "state.json"
    manager = AlertStateManager(str(path))
    url = "https://example.com"
    manager.update(_result(url, "DOWN", "t1", error="down"), 1, persist=True)
    reloaded = AlertStateManager(str(path))
    assert reloaded.get(url)["alert_fired"] is True
    assert reloaded.get(url)["outage_start"] == "t1"
    saved = json.loads(path.read_text())
    assert set(saved[url]) == {"consecutive_down", "alert_fired", "outage_start", "cooldown_remaining"}

    path.write_text("{not json")
    fresh = AlertStateManager(str(path))
    assert fresh.states == {}
    assert path.with_name("state.json.corrupt").exists()


def test_recovery_email_includes_the_outage_start():
    result = _result("https://example.com", "UP", "2026-06-04 04:56:00", code=200)
    message = build_email(result, "you@example.com", sender="bot@example.com", outage_start="2026-06-04 04:50:00", is_recovery=True)
    body = message.get_content()
    assert message["Subject"].startswith("✅ Status Monitor:")
    assert "Outage started: 2026-06-04 04:50:00" in body
    assert "Duration: 6m 0s" in body

    down = build_email(
        _result("https://example.com", "DOWN", "2026-06-04 04:50:00", error="Connection failed"),
        "you@example.com",
        sender="bot@example.com",
    )
    assert down["Subject"].startswith("🚨 Status Monitor:")
    assert "Error: Connection failed" in down.get_content()


def test_slack_and_generic_payloads():
    result = _result("https://example.com", "DOWN", "t1", error="Connection failed")
    payload = event_payload("down", result, "t1")
    assert payload["url"] == "https://example.com"
    assert payload["event"] == "down"
    slack = slack_body("down", payload)
    assert "DOWN" in slack["text"]
    assert webhook_body("generic", "down", payload) == payload
    assert infer_webhook_kind("https://hooks.slack.com/services/a/b/c") == "slack"
    assert infer_webhook_kind("https://discord.com/api/webhooks/1/2") == "discord"
    assert infer_webhook_kind("https://example.com/hook") == "generic"


def test_email_without_credentials_warns_once(monkeypatch, capsys):
    monkeypatch.delenv("SMTP_USER", raising=False)
    monkeypatch.delenv("SMTP_PASS", raising=False)
    notifier = Notifier("you@example.com", [])

    import asyncio

    result = _result("https://example.com", "DOWN", "t1", error="Connection failed")
    asyncio.run(notifier.dispatch("down", result, "t1"))
    asyncio.run(notifier.dispatch("down", result, "t1"))
    output = capsys.readouterr().out
    assert output.count("SMTP_USER") == 1


def test_webhook_post_uses_the_slack_shape():
    import asyncio

    import httpx

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content.decode())
        return httpx.Response(204)

    notifier = Notifier(None, [Webhook("https://hooks.slack.com/services/T/B/X", "slack")])
    notifier._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = _result("https://example.com", "DOWN", "t1", error="Connection failed")
    asyncio.run(notifier.dispatch("down", result, "t1"))
    asyncio.run(notifier.aclose())
    assert seen["body"]["text"]
    assert "example.com" in seen["body"]["text"]
