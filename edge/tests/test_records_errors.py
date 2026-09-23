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
