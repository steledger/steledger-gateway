"""Write admission — the only state the edge keeps (ephemeral, not a registry).

Every chain write costs the gateway wallet EMC, so before one goes out it has to
pass four checks, all here:

  - the GitHub account is at least `min_account_age_days` old;
  - per account, at most `free_tier_writes_per_min` in the trailing 60 seconds;
  - per account, at most `free_tier_writes_per_day` in the trailing 24 hours;
  - across everyone, at most `global_writes_per_day` in the trailing 24 hours.

Each window is a sorted set of write timestamps in Redis, counting only those
inside the window. A fixed calendar counter would be burst-able across its
boundary (N writes at 23:59 + N at 00:00), which a sliding window is not. All
three windows are checked and filled by one atomic Lua script, so concurrent
writes cannot race past a limit, and a write refused by one window consumes
nothing from the others.
"""
from __future__ import annotations

import logging
import secrets
import time
from datetime import datetime, timezone

import redis.asyncio as redis
from fastapi import HTTPException

from .auth import Principal
from .config import settings

log = logging.getLogger(__name__)

MINUTE = 60
DAY = 86400

# KEYS: one bucket per window.  ARGV: now, n, token, then (window, limit) per key.
# Drops expired timestamps from every bucket, and only if the `n` new writes fit
# in all of them inserts them everywhere. Returns 0 when admitted, otherwise the
# 1-based index of the first window that refused (nothing is inserted then).
_SLIDING_WINDOWS = """
local now = tonumber(ARGV[1])
local n = tonumber(ARGV[2])
local token = ARGV[3]
for i, key in ipairs(KEYS) do
  local window = tonumber(ARGV[2 + 2 * i])
  local limit = tonumber(ARGV[3 + 2 * i])
  redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window)
  if redis.call('ZCARD', key) + n > limit then
    return i
  end
end
for i, key in ipairs(KEYS) do
  local window = tonumber(ARGV[2 + 2 * i])
  for j = 1, n do
    redis.call('ZADD', key, now, token .. ':' .. j)
  end
  redis.call('PEXPIRE', key, math.ceil(window * 1000))
end
return 0
"""


def _utc_date(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


class RateLimiter:
    def __init__(self, url: str) -> None:
        self._redis = redis.from_url(url, decode_responses=True)
        self._script = self._redis.register_script(_SLIDING_WINDOWS)

    async def admit_write(self, principal: Principal, n: int = 1) -> None:
        """Admit `n` chain writes for `principal` (n>1 for a batch), or raise.

        403 for an account too new to write, 429 for a per-account limit, 503
        when the service-wide daily ceiling is reached. The detail says which,
        and when writing will work again."""
        now = time.time()
        self._check_account_age(principal, now)

        gid = principal.github_id
        windows = [
            (f"rl:nvs:{gid}", MINUTE, settings.free_tier_writes_per_min),
            (f"rl:nvs:day:{gid}", DAY, settings.free_tier_writes_per_day),
            ("rl:nvs:day:all", DAY, settings.global_writes_per_day),
        ]
        args: list = [now, n, secrets.token_hex(8)]
        for _, window, limit in windows:
            args += [window, limit]
        refused = int(await self._script(keys=[k for k, _, _ in windows], args=args))

        if refused == 1:
            raise HTTPException(
                status_code=429,
                detail=f"rate limit exceeded: {settings.free_tier_writes_per_min} writes per minute "
                "per account on this tier; retry in a minute",
                headers={"Retry-After": str(MINUTE)},
            )
        if refused == 2:
            raise HTTPException(
                status_code=429,
                detail=f"daily limit reached: {settings.free_tier_writes_per_day} writes per 24 hours "
                "per account on this tier; the window slides, so capacity returns as the oldest "
                "writes age past 24 hours",
            )
        if refused == 3:
            # Not the caller's fault and not something they can fix: say so plainly,
            # and leave a line for whoever watches the logs.
            log.warning("global daily write ceiling reached (github_id=%s, n=%s)", gid, n)
            raise HTTPException(
                status_code=503,
                detail="the service has reached its daily write capacity; reads still work, "
                "and writes resume as the last 24 hours' writes age out — try again later",
                headers={"Retry-After": "3600"},
            )

    @staticmethod
    def _check_account_age(principal: Principal, now: float) -> None:
        created = principal.github_created
        if created is None:
            return  # not a GitHub-issued token; see Principal.github_created
        eligible = created + settings.min_account_age_days * DAY
        if now < eligible:
            raise HTTPException(
                status_code=403,
                detail=f"GitHub account too new to write: accounts can write "
                f"{settings.min_account_age_days} days after creation, so this one can from "
                f"{_utc_date(eligible)} (UTC). Reading works now, with or without sign-in.",
            )

    async def ping(self) -> bool:
        return await self._redis.ping()

    async def aclose(self) -> None:
        await self._redis.aclose()
