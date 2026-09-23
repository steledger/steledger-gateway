"""Listing the records under one GitHub id — the index of an agent's memory.

`read_record` needs a full name, hash included, so an agent starting a fresh
session could learn its github_id from `whoami` and still have no way to find
what it anchored before. This lists `ai:gh:<id>` and every `ai:gh:<id>:...`.

Deliberately narrow: the only expression ever sent to the node is that fixed,
id-shaped prefix — never caller input — because each scan walks the node's
whole name index. Results are cached per id for a minute, and the adapter runs
at most two scans at once.
"""
from __future__ import annotations

import json
import re

import redis.asyncio as redis

from .client import AdapterClient

CACHE_SECONDS = 60
MEM = re.compile(r"^ai:gh:\d+:mem:(.+)$")


def _parse(raw: dict) -> dict:
    name = raw["name"]
    try:
        value = json.loads(raw.get("value") or "")
    except ValueError:
        value = raw.get("value")
    m = MEM.match(name)
    record = {
        "name": name,
        "kind": "memory" if m else "identity" if ":" not in name[len("ai:gh:"):] else "other",
        "registered_at": raw.get("registered_at"),
        "expires_in": raw.get("expires_in"),
        # The node omits `expired` on live names; say it outright either way.
        "expired": bool(raw.get("expired")),
    }
    if m:
        record["content_hash"] = m.group(1)
    if isinstance(value, dict):
        # Records written by this gateway keep the caller's part under "metadata";
        # anything else (early test writes) is shown whole rather than guessed at.
        record["metadata"] = value.get("metadata", value)
        if record["kind"] == "identity":
            record["address"] = value.get("address")
    else:
        record["metadata"] = value
    return record


class RecordLister:
    def __init__(self, adapter: AdapterClient, redis_url: str) -> None:
        self._adapter = adapter
        self._redis = redis.from_url(redis_url, decode_responses=True)

    async def list(self, github_id: int) -> list[dict]:
        """Every record under `github_id`, newest first by the block it was written in."""
        key = f"records:{github_id}"
        cached = await self._redis.get(key)
        if cached is not None:
            return json.loads(cached)
        raw = await self._adapter.filter_names(f"^ai:gh:{int(github_id)}(:|$)")
        records = sorted((_parse(r) for r in raw), key=lambda r: r["registered_at"] or 0, reverse=True)
        await self._redis.set(key, json.dumps(records), ex=CACHE_SECONDS)
        return records

    async def aclose(self) -> None:
        await self._redis.aclose()


def page(records: list[dict], github_id: int, limit: int, offset: int) -> dict:
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    chunk = records[offset:offset + limit]
    nxt = offset + len(chunk)
    return {
        "github_id": github_id,
        "total": len(records),
        "offset": offset,
        "next_offset": nxt if nxt < len(records) else None,
        "records": chunk,
    }
