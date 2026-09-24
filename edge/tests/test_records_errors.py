"""list_records parsing and paging, node-error translation, and the MCP error path.

No Redis or node needed: the lister is faked where it would touch either.
"""
from __future__ import annotations

import json

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from app import mcp_app
from app.client import AdapterError
from app.errors import from_adapter
from app.records import _parse, page

# Shapes as the production node returns them from name_filter (2026-09-23).
IDENTITY = {
    "name": "ai:gh:3772563",
    "value": '{"github_id":3772563,"github_login":"mechnotech","address":"em1qgyg",'
             '"metadata":{"note":"reference record"}}',
    "registered_at": 812215,
    "expires_in": 9586296,
}
MEMORY = {
    "name": "ai:gh:3772563:mem:9f92",
    "value": '{"github_id":3772563,"content_hash":"9f92","metadata":{"note":"decision"}}',
    "registered_at": 812384,
    "expires_in": 319374,
}
EARLY_TEST = {  # written before the gateway settled on a value shape; expired
    "name": "ai:gh:3772563:mem:demohash1",
    "value": '{"content_hash": "demohash1", "note": "batch test 1"}',
    "registered_at": 794563,
    "expires_in": -12606,
    "expired": True,
}


def test_parse_identity_and_memory():
    ident, mem = _parse(IDENTITY), _parse(MEMORY)
    assert ident["kind"] == "identity" and ident["address"] == "em1qgyg"
    assert ident["metadata"] == {"note": "reference record"} and ident["expired"] is False
    assert mem["kind"] == "memory" and mem["content_hash"] == "9f92"
    assert mem["metadata"] == {"note": "decision"}


def test_parse_keeps_odd_values_whole_and_flags_expiry():
    rec = _parse(EARLY_TEST)
    assert rec["expired"] is True
    assert rec["metadata"] == {"content_hash": "demohash1", "note": "batch test 1"}
    assert _parse({"name": "ai:gh:1:mem:x", "value": "not json"})["metadata"] == "not json"


def test_page():
    recs = [{"n": i} for i in range(5)]
    first = page(recs, 7, limit=2, offset=0)
    assert first["records"] == recs[:2] and first["next_offset"] == 2 and first["total"] == 5
    last = page(recs, 7, limit=2, offset=4)
    assert last["records"] == recs[4:] and last["next_offset"] is None
    assert page(recs, 7, limit=10_000, offset=-3)["offset"] == 0  # clamped, not trusted


@pytest.mark.parametrize("detail,code,status", [
    ("nvs write failed: there are pending operations on that name", "record_pending", 409),
    ("nvs write failed: Insufficient funds", "service_funds", 503),
    ("nvs write failed: value is too long", "value_too_large", 400),
    ("adapter unreachable: ConnectError", "node_unavailable", 503),
    ("nvs write failed: something nobody anticipated", "node_error", 502),
])
def test_node_errors_become_actionable(detail, code, status):
    err = from_adapter(AdapterError(502, detail))
    assert err.status_code == status and err.detail["error"] == code
    assert err.detail["message"] and err.detail["how_to_fix"]
    if code == "node_error":
        assert "something nobody anticipated" in err.detail["message"]  # never hidden


def test_not_found():
    assert from_adapter(AdapterError(404, "name not found: x")).detail["error"] == "not_found"


class _FailingLister:
    async def list(self, github_id):
        raise AdapterError(502, "adapter unreachable: ConnectError")


class _Lister:
    async def list(self, github_id):
        return [_parse(MEMORY), _parse(IDENTITY)]


async def _call(name, args):
    return await mcp_app.mcp.call_tool(name, args)


async def test_mcp_errors_are_json(monkeypatch):
    monkeypatch.setattr(mcp_app, "_records", _FailingLister())
    with pytest.raises(ToolError) as exc:
        await _call("list_records", {"github_id": 1})
    payload = json.loads(str(exc.value).split(": ", 1)[1])
    assert payload["error"] == "node_unavailable" and payload["retry_after"] == 300


async def test_mcp_list_records_needs_an_id_when_anonymous(monkeypatch):
    monkeypatch.setattr(mcp_app, "_records", _Lister())
    with pytest.raises(ToolError) as exc:
        await _call("list_records", {})
    assert '"github_id_required"' in str(exc.value)


async def test_mcp_list_records(monkeypatch):
    monkeypatch.setattr(mcp_app, "_records", _Lister())
    result = await _call("list_records", {"github_id": 3772563, "limit": 1})
    structured = result[1] if isinstance(result, tuple) else result
    assert structured["total"] == 2 and structured["next_offset"] == 1
    assert structured["records"][0]["content_hash"] == "9f92"


@pytest.mark.parametrize("good", [
    "9f9209756f6ace1b3f35a54869d5362776913aa8b434b66c514df216f3de3f10",  # sha256
    "d41d8cd98f00b204e9800998ecf8427e",                                  # md5
    "QmYwAPJzv5CZsnA625s3Xf2nemtYgPpHdWEz79ojWnPbdG",                    # CIDv0
    "bafybeigdyrzt5sfp7udm7hu76uh7y26nf3efuylqabf3oclgtqy55fbzdi",       # CIDv1
])
def test_hash_shapes_accepted(good):
    from app.names import mem_name
    assert mem_name(1, good).endswith(good)


@pytest.mark.parametrize("bad", ["x", "demohash1", "a" * 129, "abc:def" * 6, "has space" * 5, ""])
def test_hash_shapes_refused(bad):
    from app.errors import AgentError
    from app.names import mem_name
    with pytest.raises(AgentError) as exc:
        mem_name(1, bad)
    assert exc.value.detail["error"] == "invalid_hash"


class _CountingLimiter:
    calls = 0

    async def admit_write(self, principal, n=1):
        self.calls += 1
        return {"writes_left_this_minute": 9, "writes_left_today": 99}


async def test_bad_hash_spends_no_quota(monkeypatch):
    from app.auth import Principal
    limiter = _CountingLimiter()
    monkeypatch.setattr(mcp_app, "_ratelimiter", limiter)
    monkeypatch.setattr(mcp_app, "_principal", lambda: Principal(1, "u", "free", None))
    with pytest.raises(ToolError) as exc:
        await _call("store_memory_batch", {"records": [
            {"content_hash": "9f9209756f6ace1b3f35a54869d5362776913aa8b434b66c514df216f3de3f10"},
            {"content_hash": "nope"},
        ]})
    assert '"invalid_hash"' in str(exc.value) and limiter.calls == 0


class _HolderAdapter:
    """Every name is held elsewhere, as after transfer_records."""

    async def holder(self, name):
        return {"name": name, "state": "foreign", "address": "EHAWc65it7HFWQrMc4YHjqUfn6kxUgMvHb"}


@pytest.mark.parametrize("tool,args", [
    ("store_memory", {"content_hash": "9f9209756f6ace1b3f35a54869d5362776913aa8b434b66c514df216f3de3f10"}),
    ("store_memory_batch", {"records": [
        {"content_hash": "9f9209756f6ace1b3f35a54869d5362776913aa8b434b66c514df216f3de3f10"}]}),
    ("register_identity", {"address": "em1qgyg"}),
])
async def test_a_transferred_name_spends_no_quota(monkeypatch, tool, args):
    from app.auth import Principal
    limiter = _CountingLimiter()
    monkeypatch.setattr(mcp_app, "_ratelimiter", limiter)
    monkeypatch.setattr(mcp_app, "_adapter", _HolderAdapter())
    monkeypatch.setattr(mcp_app, "_principal", lambda: Principal(1, "u", "free", None))
    with pytest.raises(ToolError) as exc:
        await _call(tool, args)
    assert '"not_held"' in str(exc.value) and "EHAWc65" in str(exc.value)
    assert limiter.calls == 0
