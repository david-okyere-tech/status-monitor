import asyncio

import httpx
import pytest

from status_monitor.checks import check_target
from status_monitor.demo import openssl_available, start_fixtures


@pytest.mark.skipif(not openssl_available(), reason="openssl is required to mint demo certificates")
def test_demo_fixtures_classify(tmp_path):
    fixtures = start_fixtures(tmp_path / "certs")
    try:
        async def run():
            async with httpx.AsyncClient(verify=False) as client:
                return [
                    await check_target(target, http_client=client, unverified_client=client)
                    for target in fixtures.targets
                ]

        results = asyncio.run(run())
    finally:
        fixtures.stop()

    by_name = {target.name: result for target, result in zip(fixtures.targets, results)}
    assert by_name["Marketing site"].status == "UP"
    assert by_name["Docs"].status == "UP"
    assert by_name["Docs"].ssl_days_remaining is not None
    assert by_name["Docs"].ssl_days_remaining > 30
    assert by_name["Legacy admin"].status == "WARNING"
    assert "certificate" in (by_name["Legacy admin"].error or "").lower()
    assert by_name["API gateway"].status == "UP"
    assert by_name["Billing"].status == "WARNING"
    assert by_name["Billing"].status_code == 500
    assert by_name["Checkout"].status == "WARNING"
    assert "Missing content" in (by_name["Checkout"].error or "")
    assert by_name["Image CDN"].status == "WARNING"
    assert "Slow response" in (by_name["Image CDN"].error or "")
    assert by_name["Postgres"].status == "UP"
    assert by_name["Redis"].status == "DOWN"
    assert by_name["Search"].status == "DOWN"
