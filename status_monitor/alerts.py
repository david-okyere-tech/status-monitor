"""Outage state machine plus email, Slack, Discord, and generic webhooks.

Alert once per outage, then a cooldown of DOWN checks after recovery so a
flapping target cannot send a mail on every cycle. WARNING never ends an
outage and never fires a DOWN alert — only UP is recovery.

The cooldown counter keeps decrementing through the suppressed DOWN checks.
It does not reset when those checks arrive. A later UP that is not itself a
recovery clears any leftover cooldown, so an old flap cannot hide a new outage.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from email.message import EmailMessage
from typing import Callable, Optional
from urllib.parse import urlsplit

import httpx

from status_monitor.models import EMAIL_SEND_TIMEOUT, WEBHOOK_TIMEOUT, CheckResult, Webhook

logger = logging.getLogger("status_monitor")


class AlertStateManager:
    def __init__(self, state_file: str):
        self.state_file = state_file
        self.states: dict[str, dict] = {}
        self.load()

    def load(self) -> None:
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r", encoding="utf-8") as handle:
                    self.states = json.load(handle)
            except Exception as exc:
                # Corrupt state used to be discarded silently, which defeats
                # the crash-safe guarantee. Quarantine the file and start fresh.
                quarantine_path = self.state_file + ".corrupt"
                try:
                    os.replace(self.state_file, quarantine_path)
                except OSError:
                    quarantine_path = None
                message = (
                    f"⚠️  State file {self.state_file} is corrupt ({exc}) — "
                    f"starting with fresh alert state."
                    + (f" Bad file saved to {quarantine_path}." if quarantine_path else "")
                )
                print(message)
                logger.warning(message)
                self.states = {}

    def save(self) -> None:
        directory = os.path.dirname(self.state_file)
        if directory:
            os.makedirs(directory, exist_ok=True)
        temp_file = self.state_file + ".tmp"
        with open(temp_file, "w", encoding="utf-8") as handle:
            json.dump(self.states, handle)
        os.replace(temp_file, self.state_file)

    def get(self, url: str) -> dict:
        if url not in self.states:
            self.states[url] = {
                "consecutive_down": 0,
                "alert_fired": False,
                "outage_start": None,
                "cooldown_remaining": 0,
            }
        return self.states[url]

    def update(
        self,
        result: CheckResult,
        threshold: int,
        cooldown_checks: int = 3,
        persist: bool = True,
    ) -> tuple[bool, bool, Optional[str]]:
        """Return (should_alert, is_recovery, outage_start).

        `outage_start` is the timestamp captured before recovery clears it,
        so a recovery notice can still name when the outage began.
        """
        state = self.get(result.url)
        should_alert, is_recovery = False, False
        notice_outage_start: Optional[str] = None

        if result.status == "DOWN":
            if state["cooldown_remaining"] > 0:
                state["cooldown_remaining"] -= 1
                state["consecutive_down"] += 1
                if not state["outage_start"]:
                    state["outage_start"] = result.timestamp
            else:
                state["consecutive_down"] += 1
                if not state["outage_start"]:
                    state["outage_start"] = result.timestamp
                if state["consecutive_down"] >= threshold and not state["alert_fired"]:
                    state["alert_fired"] = True
                    should_alert = True
                    notice_outage_start = state["outage_start"]

        elif result.status == "UP":
            was_in_outage = state["alert_fired"]
            if was_in_outage and state["outage_start"]:
                is_recovery = True
                notice_outage_start = state["outage_start"]
                state["cooldown_remaining"] = cooldown_checks
            else:
                state["cooldown_remaining"] = 0
            state["consecutive_down"] = 0
            state["alert_fired"] = False
            state["outage_start"] = None

        else:  # WARNING — reachable, but not a recovery
            state["consecutive_down"] = 0

        if persist:
            self.save()
        return should_alert, is_recovery, notice_outage_start


def build_email(
    result: CheckResult,
    recipient: str,
    *,
    sender: str,
    outage_start: Optional[str] = None,
    is_recovery: bool = False,
) -> EmailMessage:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    name = result.name or result.url
    if is_recovery:
        message["Subject"] = f"✅ Status Monitor: {result.url} is back UP"
        duration = _duration_line(outage_start, result.timestamp)
        message.set_content(
            f"URL: {result.url}\n"
            f"Name: {name}\n"
            f"Outage started: {outage_start}\n"
            f"Recovered at: {result.timestamp}\n"
            f"{duration}"
        )
    else:
        message["Subject"] = f"🚨 Status Monitor: {result.url} is DOWN"
        message.set_content(
            f"URL: {result.url}\n"
            f"Name: {name}\n"
            f"Time: {result.timestamp}\n"
            f"Error: {result.error}\n"
        )
    return message


def event_payload(event: str, result: CheckResult, outage_start: Optional[str]) -> dict:
    response_ms = None if result.response_time_ms is None else round(result.response_time_ms, 1)
    return {
        "event": event,
        "monitor": "status-monitor",
        "name": result.name or result.url,
        "url": result.url,
        "status": result.status,
        "status_code": result.status_code,
        "response_time_ms": response_ms,
        "error": result.error,
        "timestamp": result.timestamp,
        "outage_start": outage_start,
        "check_type": result.check_type,
    }


def slack_body(event: str, payload: dict) -> dict:
    mark = ":red_circle:" if event == "down" else ":large_green_circle:"
    verb = "is DOWN" if event == "down" else "is back UP"
    text = f"{mark} *{payload['name']}* {verb}\n{payload['url']}"
    if payload.get("error") and event == "down":
        text += f"\n{payload['error']}"
    if event == "recovery" and payload.get("outage_start"):
        text += f"\nOutage started: {payload['outage_start']}"
    return {"text": text}


def discord_body(event: str, payload: dict) -> dict:
    color = 0xFF6A45 if event == "down" else 0xC6F135
    title = "DOWN" if event == "down" else "Recovered"
    description = payload.get("error") or payload["status"]
    fields = [
        {"name": "URL", "value": payload["url"][:1000], "inline": False},
        {"name": "When", "value": str(payload["timestamp"]), "inline": True},
    ]
    if event == "recovery" and payload.get("outage_start"):
        fields.append({"name": "Outage started", "value": str(payload["outage_start"]), "inline": True})
    return {
        "embeds": [
            {
                "title": f"{title}: {payload['name']}"[:250],
                "description": str(description)[:2000],
                "color": color,
                "fields": fields,
            }
        ]
    }


def webhook_body(kind: str, event: str, payload: dict) -> dict:
    if kind == "slack":
        return slack_body(event, payload)
    if kind == "discord":
        return discord_body(event, payload)
    return payload


class Notifier:
    def __init__(
        self,
        email: Optional[str],
        webhooks: list[Webhook],
        output: Optional[Callable[[str], None]] = None,
    ):
        self.email = email
        self.webhooks = list(webhooks)
        self.output = output or (lambda message: print(message))
        self._client: Optional[httpx.AsyncClient] = None
        self._smtp_warned = False

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def dispatch(self, event: str, result: CheckResult, outage_start: Optional[str]) -> None:
        if event not in {"down", "recovery"}:
            return
        tasks = []
        if self.email:
            tasks.append(self._send_email(event, result, outage_start))
        for hook in self.webhooks:
            tasks.append(self._send_webhook(hook, event, result, outage_start))
        if tasks:
            await asyncio.gather(*tasks)

    async def _send_email(self, event: str, result: CheckResult, outage_start: Optional[str]) -> None:
        host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
        port = int(os.environ.get("SMTP_PORT", "587"))
        user = os.environ.get("SMTP_USER", "")
        password = os.environ.get("SMTP_PASS", "").replace(" ", "")
        use_tls = os.environ.get("SMTP_TLS", "true").lower() != "false"
        if not user or not password:
            if not self._smtp_warned:
                self._smtp_warned = True
                self.output("  ⚠️  Email alerts skipped: set SMTP_USER and SMTP_PASS")
            return
        assert self.email is not None
        message = build_email(
            result,
            self.email,
            sender=user,
            outage_start=outage_start,
            is_recovery=event == "recovery",
        )
        try:
            import aiosmtplib

            await asyncio.wait_for(
                aiosmtplib.send(
                    message,
                    hostname=host,
                    port=port,
                    username=user,
                    password=password,
                    start_tls=use_tls,
                    validate_certs=True,
                ),
                timeout=EMAIL_SEND_TIMEOUT,
            )
            label = "Recovery" if event == "recovery" else "Alert"
            self.output(f"  📧 {label} email sent to {self.email}")
        except asyncio.TimeoutError:
            self.output(f"  ⚠️  Email failed: SMTP send timed out after {EMAIL_SEND_TIMEOUT}s")
        except Exception as exc:
            self.output(f"  ⚠️  Email failed: {exc}")

    async def _send_webhook(
        self,
        hook: Webhook,
        event: str,
        result: CheckResult,
        outage_start: Optional[str],
    ) -> None:
        payload = event_payload(event, result, outage_start)
        body = webhook_body(hook.kind, event, payload)
        try:
            client = await self._http()
            response = await client.post(hook.url, json=body)
            response.raise_for_status()
            label = "Recovery" if event == "recovery" else "Alert"
            self.output(f"  📨 {label} webhook sent ({hook.kind}: {_hook_host(hook.url)})")
        except Exception as exc:
            self.output(f"  ⚠️  Webhook failed ({_hook_host(hook.url)}): {exc}")

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=WEBHOOK_TIMEOUT, follow_redirects=False)
        return self._client


def _hook_host(url: str) -> str:
    return urlsplit(url).netloc or "webhook"


def _duration_line(start: Optional[str], end: str) -> str:
    if not start:
        return ""
    try:
        fmt = "%Y-%m-%d %H:%M:%S"
        from datetime import datetime

        delta = datetime.strptime(end, fmt) - datetime.strptime(start, fmt)
    except ValueError:
        return ""
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return ""
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        pretty = f"{hours}h {minutes}m"
    elif minutes:
        pretty = f"{minutes}m {sec}s"
    else:
        pretty = f"{sec}s"
    return f"Duration: {pretty}\n"
