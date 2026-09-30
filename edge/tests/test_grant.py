"""Fee-grant reservation against a real Redis — once per account, a daily ceiling.

    docker run -d --rm -p 6390:6379 redis:7
    EDGE_TEST_REDIS_URL=redis://localhost:6390/15 EDGE_JWT_SECRET=$(printf 'x%.0s' {1..40}) \
        python -m pytest edge/tests

Skipped without EDGE_TEST_REDIS_URL. The database it points at is flushed.
"""
from __future__ import annotations

import os

import pytest
import redis.asyncio as redis

from app.config import settings
from app.errors import AgentError
from app.grant import FeeGrants

URL = os.environ.get("EDGE_TEST_REDIS_URL")
needs_redis = pytest.mark.skipif(not URL, reason="EDGE_TEST_REDIS_URL not set")


@pytest.fixture
async def grants(monkeypatch):
    monkeypatch.setattr(settings, "fee_grants_per_day", 2)
    r = redis.from_url(URL)
    await r.flushdb()
    await r.aclose()
    g = FeeGrants(URL)
    yield g
    await g.aclose()


async def refused(g: FeeGrants, gid: int) -> str:
    with pytest.raises(AgentError) as info:
        await g.reserve(gid)
    return info.value.detail["error"]


@needs_redis
async def test_once_per_account_for_good(grants):
    token = await grants.reserve(1)
    await grants.settle(1, "em1qx", "tx")
    assert token
    assert await refused(grants, 1) == "fee_grant_used"


@needs_redis
async def test_paid_grants_are_counted_per_day(grants):
    await grants.reserve(1)
    await grants.settle(1, "em1qx", "tx")
    await grants.failed()
    r = redis.from_url(URL, decode_responses=True)
    assert list((await r.hgetall("grant:daily")).values()) == ["1"]
    assert float(list((await r.hgetall("grant:emc:daily")).values())[0]) == 0.01
    assert list((await r.hgetall("grant:failed:daily")).values()) == ["1"]
    await r.aclose()


@needs_redis
async def test_a_second_reserve_before_settling_is_refused(grants):
    await grants.reserve(1)
    assert await refused(grants, 1) == "fee_grant_used"


@needs_redis
async def test_daily_ceiling_across_accounts(grants):
    await grants.reserve(1)
    await grants.reserve(2)
    assert await refused(grants, 3) == "fee_grant_unavailable"


@needs_redis
async def test_release_frees_both_the_account_and_the_budget(grants):
    await grants.reserve(1)
    t2 = await grants.reserve(2)
    await grants.release(2, t2)
    await grants.reserve(2)          # the account is free again
    assert await refused(grants, 3) == "fee_grant_unavailable"


@needs_redis
async def test_off_when_ceiling_is_zero(grants, monkeypatch):
    monkeypatch.setattr(settings, "fee_grants_per_day", 0)
    assert await refused(grants, 1) == "fee_grant_unavailable"
