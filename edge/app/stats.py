"""MCP usage statistics — lightweight Redis counters.

Records one event per remote-MCP tools/call: which tool, whether a caller was
authenticated (counted as a unique github_id, never stored by name in the public
view), the client (HTTP User-Agent), and a per-day total. Best-effort: a Redis
hiccup must never break a tool call. The public snapshot exposes aggregates only —
no per-user identities.

Refusals and failures are counted too — over MCP and REST — by where they
happened and their stable error code (see errors.py), so it shows where agents
get stuck and not only where they succeed. An error event holds the tool or
route, the code, the client and the time: never the caller, never the
arguments. `internal_error` is the one that means a bug on our side.
"""
from __future__ import annotations

import json
import logging
import time

import redis.asyncio as redis

log = logging.getLogger("edge.stats")

_RECENT_MAX = 200
_ERROR_DAYS_KEPT = 35 * 86400  # per-day breakdowns; the all-time totals stay
INTERNAL = "internal_error"


class Stats:
    def __init__(self, url: str) -> None:
        self._redis = redis.from_url(url, decode_responses=True)

    async def record_call(self, tool: str, github_id: int | None, client: str) -> None:
        """Best-effort: increment the counters for one MCP tools/call."""
        try:
            ts = int(time.time())
            day = time.strftime("%Y-%m-%d", time.gmtime(ts))
            pipe = self._redis.pipeline()
            pipe.incr("mcp:total")
            pipe.hincrby("mcp:tools", tool, 1)
            pipe.hincrby("mcp:daily", day, 1)
            if github_id is not None:
                pipe.sadd("mcp:callers", github_id)
            if client:
                pipe.hincrby("mcp:clients", client[:80], 1)
            pipe.lpush("mcp:recent", json.dumps({"ts": ts, "tool": tool, "client": client[:80]}))
            pipe.ltrim("mcp:recent", 0, _RECENT_MAX - 1)
            await pipe.execute()
        except Exception as exc:  # noqa: BLE001 — stats must never break a call
            log.warning("stats record failed: %s", exc)

    async def record_error(self, where: str, code: str, client: str) -> None:
        """Best-effort: count one refusal or failure at `where` (a tool or a route)."""
        try:
            ts = int(time.time())
            day = time.strftime("%Y-%m-%d", time.gmtime(ts))
            key = f"{where}:{code}"
            pipe = self._redis.pipeline()
            pipe.hincrby("mcp:errors", key, 1)
            pipe.hincrby("mcp:errors:daily", day, 1)
            pipe.hincrby(f"mcp:errors:day:{day}", key, 1)
            pipe.expire(f"mcp:errors:day:{day}", _ERROR_DAYS_KEPT)
            if code == INTERNAL:
                pipe.incr("mcp:errors:internal")
            pipe.lpush("mcp:errors:recent", json.dumps(
                {"ts": ts, "where": where, "code": code, "client": client[:80]}))
            pipe.ltrim("mcp:errors:recent", 0, _RECENT_MAX - 1)
            await pipe.execute()
        except Exception as exc:  # noqa: BLE001 — stats must never break a call
            log.warning("stats error record failed: %s", exc)

    async def snapshot(self) -> dict:
        """Aggregate view (no per-user identities)."""
        try:
            pipe = self._redis.pipeline()
            pipe.get("mcp:total")
            pipe.hgetall("mcp:tools")
            pipe.hgetall("mcp:daily")
            pipe.scard("mcp:callers")
            pipe.hgetall("mcp:clients")
            pipe.lrange("mcp:recent", 0, 49)
            pipe.hgetall("mcp:errors")
            pipe.hgetall("mcp:errors:daily")
            pipe.lrange("mcp:errors:recent", 0, 49)
            (total, tools, daily, callers, clients, recent,
             errors, errors_daily, errors_recent) = await pipe.execute()
        except Exception as exc:  # noqa: BLE001
            log.warning("stats snapshot failed: %s", exc)
            return {"error": "stats unavailable"}
        return {
            "total_calls": int(total or 0),
            "unique_callers": int(callers or 0),
            "tools": {k: int(v) for k, v in (tools or {}).items()},
            "clients": {k: int(v) for k, v in (clients or {}).items()},
            "daily": {k: int(v) for k, v in (daily or {}).items()},
            "recent": [json.loads(x) for x in (recent or [])],
            "errors": {
                "total": sum(int(v) for v in (errors or {}).values()),
                "by_code": {k: int(v) for k, v in (errors or {}).items()},
                "daily": {k: int(v) for k, v in (errors_daily or {}).items()},
                "recent": [json.loads(x) for x in (errors_recent or [])],
            },
        }

    async def aclose(self) -> None:
        await self._redis.aclose()
