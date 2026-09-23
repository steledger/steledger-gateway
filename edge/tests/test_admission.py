"""Write admission against a real Redis — the Lua script is the part worth testing.

    docker run -d --rm -p 6390:6379 redis:7
    EDGE_TEST_REDIS_URL=redis://localhost:6390/15 EDGE_JWT_SECRET=$(printf 'x%.0s' {1..40}) \
        python -m pytest edge/tests

Skipped without EDGE_TEST_REDIS_URL. The database it points at is flushed.
"""
from __future__ import annotations

import os
import time

import pytest
import redis.asyncio as redis
from fastapi import HTTPException

from app.auth import Principal, decode_token, issue_jwt
from app.config import settings
from app.github import parse_created
from app.ratelimit import DAY, RateLimiter

URL = os.environ.get("EDGE_TEST_REDIS_URL")
needs_redis = pytest.mark.skipif(not URL, reason="EDGE_TEST_REDIS_URL not set")

OLD = int(time.time()) - 400 * DAY


def who(gid: int, created: int | None = OLD) -> Principal:
    return Principal(github_id=gid, github_login=f"u{gid}", tariff="free", github_created=created)


@pytest.fixture
async def rl(monkeypatch):
    monkeypatch.setattr(settings, "free_tier_writes_per_min", 3)
    monkeypatch.setattr(settings, "free_tier_writes_per_day", 5)
    monkeypatch.setattr(settings, "global_writes_per_day", 8)
    monkeypatch.setattr(settings, "min_account_age_days", 30)
    r = redis.from_url(URL)
    await r.flushdb()
    limiter = RateLimiter(URL)
    yield limiter
    await limiter.aclose()
    await r.flushdb()
    await r.aclose()


async def zcard(key: str) -> int:
    r = redis.from_url(URL)
    try:
        return await r.zcard(key)
    finally:
        await r.aclose()


async def refused(coro) -> HTTPException:
    with pytest.raises(HTTPException) as exc:
        await coro
    return exc.value


@needs_redis
async def test_minute_limit_refuses_without_consuming_the_day(rl):
    for _ in range(3):
        await rl.admit_write(who(1))
    exc = await refused(rl.admit_write(who(1)))
    assert exc.status_code == 429 and "per minute" in exc.detail
    assert await zcard("rl:nvs:day:1") == 3
    assert await zcard("rl:nvs:day:all") == 3


@needs_redis
async def test_daily_limit_per_account(rl, monkeypatch):
    monkeypatch.setattr(settings, "free_tier_writes_per_min", 100)
    await rl.admit_write(who(1), 5)
    exc = await refused(rl.admit_write(who(1)))
    assert exc.status_code == 429 and "24 hours" in exc.detail
    await rl.admit_write(who(2))  # another account is unaffected


@needs_redis
async def test_batch_counts_every_record(rl, monkeypatch):
    monkeypatch.setattr(settings, "free_tier_writes_per_min", 100)
    await rl.admit_write(who(1), 4)
    exc = await refused(rl.admit_write(who(1), 2))  # 4 + 2 > 5
    assert exc.status_code == 429
    await rl.admit_write(who(1), 1)  # exactly at the cap is fine


@needs_redis
async def test_global_ceiling_is_shared_and_refuses_with_503(rl, monkeypatch):
    monkeypatch.setattr(settings, "free_tier_writes_per_min", 100)
    await rl.admit_write(who(1), 5)
    await rl.admit_write(who(2), 3)  # 8 total: the ceiling
    exc = await refused(rl.admit_write(who(3)))
    assert exc.status_code == 503 and exc.headers["Retry-After"]
    # the refused write took nothing from the account's own windows
    assert await zcard("rl:nvs:3") == 0
    assert await zcard("rl:nvs:day:3") == 0


@needs_redis
async def test_window_slides(rl, monkeypatch):
    await rl.admit_write(who(1), 3)
    later = time.time() + 61
    monkeypatch.setattr("app.ratelimit.time.time", lambda: later)
    await rl.admit_write(who(1))  # the minute window has emptied


@needs_redis
async def test_account_age(rl):
    now = int(time.time())
    exc = await refused(rl.admit_write(who(1, created=now - 29 * DAY)))
    assert exc.status_code == 403 and "too new" in exc.detail
    assert await zcard("rl:nvs:day:all") == 0  # refused before any window
    await rl.admit_write(who(2, created=now - 31 * DAY))
    await rl.admit_write(who(3, created=None))  # signature-login token: no claim


def test_created_claim_round_trips():
    tok = issue_jwt(7, "seven", github_created=1_300_000_000)
    assert decode_token(tok).github_created == 1_300_000_000
    assert decode_token(issue_jwt(7, "seven")).github_created is None


def test_parse_created():
    assert parse_created("2011-01-25T18:44:36Z") == 1295981076
    assert parse_created(None) is None
    assert parse_created("garbage") is None
