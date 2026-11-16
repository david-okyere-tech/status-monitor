"""HTTP, TCP, ICMP, content, and TLS-expiry checks.

HTTP classification matches v4: an expected status is UP, an unexpected
status after retries is WARNING (the host answered), and a connection
failure or timeout is DOWN. Content, latency, and certificate warnings
only downgrade an UP result — they never hide an outage.
"""

from __future__ import annotations

import asyncio
import random
import re
import socket
import ssl
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlsplit

import httpx

from status_monitor.models import CheckResult, TargetConfig

_PING_TIME = re.compile(r"time[=<]\s*([\d.]+)\s*ms", re.IGNORECASE)
_CONTENT_LIMIT = 512 * 1024


def make_result(
    target: TargetConfig,
    status: str,
    status_code: Optional[int],
    response_time_ms: Optional[float],
    error: Optional[str],
    attempt: int,
    *,
    ssl_days_remaining: Optional[int] = None,
) -> CheckResult:
    return CheckResult(
        url=target.url,
        timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        status=status,
        status_code=status_code,
        response_time_ms=None if response_time_ms is None else float(response_time_ms),
        error=error,
        attempt=attempt,
        name=target.name or target.url,
        check_type=target.check_type,
        ssl_days_remaining=ssl_days_remaining,
        ts_unix=time.time(),
    )


def connect_error_message(exc: BaseException) -> str:
    message = str(exc)
    lowered = message.lower()
    if "ssl" in lowered or "tls" in lowered or "certificate" in lowered:
        return f"TLS error: {message}"
    return "Connection failed"


def classify_ping(stdout: str, returncode: int) -> tuple[str, Optional[float], Optional[str]]:
    match = _PING_TIME.search(stdout)
    elapsed = float(match.group(1)) if match else None
    if returncode == 0:
        return "UP", elapsed, None
    lowered = stdout.lower()
    if "unknown host" in lowered or "name or service not known" in lowered or "not known" in lowered:
        return "DOWN", None, "Unknown host"
    if "100%" in stdout and "packet loss" in lowered:
        return "DOWN", None, "Ping failed: 100% packet loss"
    return "DOWN", None, "Ping failed"


def apply_success_checks(result: CheckResult, target: TargetConfig, body: Optional[str]) -> None:
    """Downgrade UP to WARNING when content or latency misses the target."""
    if result.status != "UP":
        return
    if body is not None and target.contains:
        for needle in target.contains:
            if needle not in body:
                result.status = "WARNING"
                result.error = f"Missing content: {needle}"
                return
    if body is not None and target.regex:
        if re.search(target.regex, body) is None:
            result.status = "WARNING"
            result.error = f"Content did not match /{target.regex}/"
            return
    if (
        target.max_latency_ms is not None
        and result.response_time_ms is not None
        and result.response_time_ms > target.max_latency_ms
    ):
        result.status = "WARNING"
        result.error = (
            f"Slow response: {result.response_time_ms:.0f}ms "
            f"(limit {target.max_latency_ms:.0f}ms)"
        )


def apply_cert_warning(result: CheckResult, target: TargetConfig) -> None:
    days = result.ssl_days_remaining
    if result.status != "UP" or target.ssl_warn_days is None or days is None:
        return
    if days < target.ssl_warn_days:
        result.status = "WARNING"
        if days < 0:
            result.error = f"TLS certificate expired {abs(days)} day(s) ago"
        else:
            result.error = f"TLS certificate expires in {days} day(s)"


async def _sleep_backoff(attempt: int) -> None:
    delay = min(30, (2 ** attempt) + random.uniform(0, 1))
    await asyncio.sleep(delay)


async def check_target(
    target: TargetConfig,
    *,
    http_client: Optional[httpx.AsyncClient] = None,
    unverified_client: Optional[httpx.AsyncClient] = None,
) -> CheckResult:
    try:
        if target.check_type == "tcp":
            return await check_tcp(target)
        if target.check_type == "ping":
            return await check_ping(target)
        client = http_client if target.verify_ssl else unverified_client
        if client is None:
            return make_result(target, "DOWN", None, None, "HTTP client is not configured", 1)
        return await check_http(client, target)
    except Exception as exc:  # one bad target must not kill the cycle
        return make_result(target, "DOWN", None, None, f"Check error: {exc}", target.retries + 1)


async def check_http(client: httpx.AsyncClient, target: TargetConfig) -> CheckResult:
    max_attempts = target.retries + 1
    last_error = "Check failed"
    ssl_task = None
    if urlsplit(target.url).scheme == "https":
        host, port = _http_host_port(target)
        ssl_task = asyncio.create_task(probe_cert_days(host, port, target.timeout))

    result: Optional[CheckResult] = None
    try:
        for attempt in range(1, max_attempts + 1):
            try:
                started = time.monotonic()
                headers = {"User-Agent": target.user_agent}
                headers.update(target.headers)
                response = await client.request(
                    target.method,
                    target.url,
                    timeout=target.timeout,
                    follow_redirects=target.follow_redirects,
                    headers=headers,
                )
                elapsed_ms = (time.monotonic() - started) * 1000
                if response.status_code in target.expected_statuses:
                    result = make_result(target, "UP", response.status_code, elapsed_ms, None, attempt)
                    body = _body_snippet(response) if target.contains or target.regex else None
                    apply_success_checks(result, target, body)
                    break
                last_error = f"Expected {target.expected_statuses}, got {response.status_code}"
                if attempt < max_attempts:
                    await _sleep_backoff(attempt)
                    continue
                result = make_result(target, "WARNING", response.status_code, elapsed_ms, last_error, attempt)
                break
            except httpx.ConnectError as exc:
                last_error = connect_error_message(exc)
            except httpx.TimeoutException:
                last_error = "Request timed out"
            except httpx.TooManyRedirects:
                last_error = "Too many redirects"
            except httpx.RequestError as exc:
                last_error = str(exc) or "Request failed"

            if attempt < max_attempts:
                await _sleep_backoff(attempt)
        if result is None:
            result = make_result(target, "DOWN", None, None, last_error, max_attempts)
    finally:
        if ssl_task is not None:
            days = await _ssl_task_result(ssl_task)
            result_holder = result
            if result_holder is not None:
                result_holder.ssl_days_remaining = days
                apply_cert_warning(result_holder, target)
    assert result is not None
    return result


async def check_tcp(target: TargetConfig) -> CheckResult:
    max_attempts = target.retries + 1
    last_error = "Connection failed"
    for attempt in range(1, max_attempts + 1):
        writer = None
        try:
            started = time.monotonic()
            _reader, writer = await asyncio.wait_for(
                asyncio.open_connection(target.host, target.port),
                timeout=target.timeout,
            )
            elapsed_ms = (time.monotonic() - started) * 1000
            result = make_result(target, "UP", None, elapsed_ms, None, attempt)
            apply_success_checks(result, target, None)
            return result
        except asyncio.TimeoutError:
            last_error = "Connection timed out"
        except ConnectionRefusedError:
            last_error = "Connection refused"
        except OSError as exc:
            last_error = str(exc) or "Connection failed"
        finally:
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
        if attempt < max_attempts:
            await _sleep_backoff(attempt)
    return make_result(target, "DOWN", None, None, last_error, max_attempts)


async def check_ping(target: TargetConfig) -> CheckResult:
    max_attempts = target.retries + 1
    last_error = "Ping failed"
    wait_seconds = max(1, int(target.timeout if target.timeout >= 1 else 1))
    for attempt in range(1, max_attempts + 1):
        try:
            started = time.monotonic()
            process = await asyncio.create_subprocess_exec(
                "ping",
                "-c",
                "1",
                "-W",
                str(wait_seconds),
                target.host,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except FileNotFoundError:
            return make_result(target, "DOWN", None, None, "ping command not available", attempt)
        try:
            stdout, _stderr = await asyncio.wait_for(process.communicate(), timeout=target.timeout + 2)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            last_error = "Ping timed out"
            if attempt < max_attempts:
                await _sleep_backoff(attempt)
                continue
            return make_result(target, "DOWN", None, None, last_error, max_attempts)

        text = (stdout or b"").decode("utf-8", "replace")
        status, elapsed, error = classify_ping(text, process.returncode or 0)
        if status == "UP":
            measured = elapsed if elapsed is not None else (time.monotonic() - started) * 1000
            result = make_result(target, "UP", None, measured, None, attempt)
            apply_success_checks(result, target, None)
            return result
        last_error = error or "Ping failed"
        if attempt < max_attempts:
            await _sleep_backoff(attempt)
    return make_result(target, "DOWN", None, None, last_error, max_attempts)


async def probe_cert_days(host: str, port: int, timeout: float) -> Optional[int]:
    """Return whole days until the peer certificate expires, or None."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    server_hostname = _sni_hostname(host)
    writer = None
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=context, server_hostname=server_hostname),
            timeout=timeout,
        )
        ssl_object = writer.get_extra_info("ssl_object")
        if ssl_object is None:
            return None
        der = ssl_object.getpeercert(True)
        if not der:
            return None
        _not_before, not_after = extract_validity(der)
        return days_until(not_after)
    except Exception:
        return None
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass


def extract_validity(der: bytes) -> tuple[datetime, datetime]:
    """Pull notBefore/notAfter out of a DER certificate without extra deps."""
    parser = _Der(der)
    tag, certificate = parser.read_tlv()
    if tag != 0x30:
        raise ValueError("not a certificate")
    body = _Der(certificate)
    tag, tbs = body.read_tlv()
    if tag != 0x30:
        raise ValueError("missing TBS certificate")
    tbs_parser = _Der(tbs)
    if tbs_parser.remaining() and tbs_parser.peek() == 0xA0:
        tbs_parser.skip_tlv()
    tbs_parser.skip_tlv()  # serial
    tbs_parser.skip_tlv()  # signature algorithm
    tbs_parser.skip_tlv()  # issuer
    tag, validity = tbs_parser.read_tlv()
    if tag != 0x30:
        raise ValueError("missing validity")
    validity_parser = _Der(validity)
    not_before = _parse_time(validity_parser)
    not_after = _parse_time(validity_parser)
    return not_before, not_after


def days_until(not_after: datetime, now: Optional[datetime] = None) -> int:
    if now is None:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
    return (not_after - now).days


class _Der:
    def __init__(self, data: bytes):
        self.data = data
        self.i = 0

    def remaining(self) -> int:
        return len(self.data) - self.i

    def peek(self) -> int:
        return self.data[self.i]

    def read_tlv(self) -> tuple[int, bytes]:
        if self.i >= len(self.data):
            raise ValueError("truncated DER")
        tag = self.data[self.i]
        self.i += 1
        length, self.i = _read_length(self.data, self.i)
        end = self.i + length
        if end > len(self.data):
            raise ValueError("truncated DER value")
        value = self.data[self.i:end]
        self.i = end
        return tag, value

    def skip_tlv(self) -> None:
        self.read_tlv()


def _read_length(data: bytes, index: int) -> tuple[int, int]:
    if index >= len(data):
        raise ValueError("truncated DER length")
    first = data[index]
    index += 1
    if first < 0x80:
        return first, index
    count = first & 0x7F
    if count == 0 or count > 3 or index + count > len(data):
        raise ValueError("bad DER length")
    length = int.from_bytes(data[index:index + count], "big")
    return length, index + count


def _parse_time(parser: _Der) -> datetime:
    tag, value = parser.read_tlv()
    text = value.decode("ascii")
    if text.endswith("Z"):
        text = text[:-1]
    text = text.split(".", 1)[0]
    if tag == 0x17:  # UTCTime YYMMDDHHMMSSZ
        return datetime.strptime(text[:12], "%y%m%d%H%M%S")
    if tag == 0x18:  # GeneralizedTime
        if len(text) >= 14:
            return datetime.strptime(text[:14], "%Y%m%d%H%M%S")
        if len(text) >= 12:
            return datetime.strptime(text[:12], "%Y%m%d%H%M")
    raise ValueError(f"unsupported time tag {tag:#x}")


def _body_snippet(response: httpx.Response) -> str:
    raw = response.content[:_CONTENT_LIMIT]
    encoding = response.encoding or "utf-8"
    return raw.decode(encoding, errors="replace")


def _http_host_port(target: TargetConfig) -> tuple[str, int]:
    parts = urlsplit(target.url)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return parts.hostname or target.host, port


def _sni_hostname(host: str) -> Optional[str]:
    try:
        socket.inet_aton(host)
        return None
    except OSError:
        pass
    if ":" in host:
        return None
    return host


async def _ssl_task_result(task: asyncio.Task) -> Optional[int]:
    try:
        return await task
    except Exception:
        return None
