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

from .auth import Principal
from .config import settings
from .errors import AgentError

log = logging.getLogger(__name__)

MINUTE = 60
DAY = 86400

# KEYS: one bucket per window.  ARGV: now, n, token, then (window, limit) per key.
# Drops expired timestamps from every bucket, and only if the `n` new writes fit
# in all of them inserts them everywhere. Returns 0 when admitted, otherwise the
# 1-based index of the first window that refused (nothing is inserted then).
# On success it returns {0, count in window 1, count in window 2}, so the caller
# can say how much is left without a second round trip.
_SLIDING_WINDOWS = """
local now = tonumber(ARGV[1])
local n = tonumber(ARGV[2])
local token = ARGV[3]
for i, key in ipairs(KEYS) do
  local window = tonumber(ARGV[2 + 2 * i])
  local limit = tonumber(ARGV[3 + 2 * i])
  redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window)
  if redis.call('ZCARD', key) + n > limit then
    return {i}
  end
end
for i, key in ipairs(KEYS) do
  local window = tonumber(ARGV[2 + 2 * i])
  for j = 1, n do
    redis.call('ZADD', key, now, token .. ':' .. j)
  end
  redis.call('PEXPIRE', key, math.ceil(window * 1000))
end
return {0, redis.call('ZCARD', KEYS[1]), redis.call('ZCARD', KEYS[2])}
"""


def _utc_date(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def _quota(used_minute: int, used_day: int) -> dict:
    return {
        "writes_left_this_minute": max(0, settings.free_tier_writes_per_min - used_minute),
        "writes_left_today": max(0, settings.free_tier_writes_per_day - used_day),
    }


class RateLimiter:
    def __init__(self, url: str) -> None:
        self._redis = redis.from_url(url, decode_responses=True)
        self._script = self._redis.register_script(_SLIDING_WINDOWS)

    async def admit_write(self, principal: Principal, n: int = 1) -> dict:
        """Admit `n` chain writes for `principal` (n>1 for a batch), or raise.

        403 for an account too new to write, 429 for a per-account limit, 503
        when the service-wide daily ceiling is reached. The detail says which,
        and when writing will work again. On success, returns what is left:
        {"writes_left_this_minute": .., "writes_left_today": ..}."""
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
        result = await self._script(keys=[k for k, _, _ in windows], args=args)
        refused = int(result[0])

        if refused == 1:
            raise AgentError(
                429, "rate_limited",
                f"This account has made {settings.free_tier_writes_per_min} writes in the last minute, "
                "the most the free tier allows.",
                "Wait a minute, or batch memory records into one write.",
                retry_after=MINUTE,
            )
        if refused == 2:
            raise AgentError(
                429, "daily_limit",
                f"This account has made {settings.free_tier_writes_per_day} writes in the last 24 hours, "
                "the most the free tier allows.",
                "The window slides: capacity comes back as the oldest of those writes turn 24 hours old.",
                retry_after=3600,
            )
        if refused == 3:
            # Not the caller's fault and not something they can fix: say so plainly,
            # and leave a line for whoever watches the logs.
            log.warning("global daily write ceiling reached (github_id=%s, n=%s)", gid, n)
            raise AgentError(
                503, "service_capacity",
                "The service has reached its daily write capacity. Nothing is wrong on your side.",
                "Retry later; writes resume as the last 24 hours' writes age out. Reads still work.",
                retry_after=3600,
            )
        return _quota(int(result[1]), int(result[2]))

    async def remaining(self, principal: Principal) -> dict:
        """What `admit_write` would leave, without writing anything. Stale entries
        are skipped by score rather than removed — this path never modifies state."""
        now = time.time()
        gid = principal.github_id
        minute = await self._redis.zcount(f"rl:nvs:{gid}", now - MINUTE, "+inf")
        day = await self._redis.zcount(f"rl:nvs:day:{gid}", now - DAY, "+inf")
        quota = _quota(minute, day)
        created = principal.github_created
        if created is not None and now < created + settings.min_account_age_days * DAY:
            quota["writes_open_on"] = _utc_date(created + settings.min_account_age_days * DAY)
        return quota

    @staticmethod
    def _check_account_age(principal: Principal, now: float) -> None:
        created = principal.github_created
        if created is None:
            return  # not a GitHub-issued token; see Principal.github_created
        eligible = created + settings.min_account_age_days * DAY
        if now < eligible:
            raise AgentError(
                403, "account_too_new",
                f"GitHub accounts can write {settings.min_account_age_days} days after they are "
                f"created; this one can from {_utc_date(eligible)} (UTC).",
                "Reading works now, with or without sign-in. Writes open on that date.",
                retry_after=int(eligible - now),
            )

    async def ping(self) -> bool:
        return await self._redis.ping()

    async def aclose(self) -> None:
        await self._redis.aclose()
