"""Refusals and failures are counted by code, over MCP and REST, and a bug
reaches the agent as `internal_error` rather than a traceback."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from mcp.server.fastmcp.exceptions import ToolError

from app import main, mcp_app
from app.auth import Principal, current_principal

HASH = "9f9209756f6ace1b3f35a54869d5362776913aa8b434b66c514df216f3de3f10"


class FakeStats:
    def __init__(self):
        self.calls, self.errors = [], []

    async def record_call(self, tool, github_id, client):
        self.calls.append(tool)

    async def record_error(self, where, code, client):
        self.errors.append((where, code))


class BrokenAdapter:
    async def holder(self, name):
        raise RuntimeError("something nobody anticipated")


class Limiter:
    async def admit_write(self, principal, n=1):
        return {}


async def _call(name, args):
    return await mcp_app.mcp.call_tool(name, args)


@pytest.fixture
def stats(monkeypatch):
    s = FakeStats()
    monkeypatch.setattr(mcp_app, "_stats", s)
    return s


async def test_mcp_refusal_is_counted_by_code(stats, monkeypatch):
    monkeypatch.setattr(mcp_app, "_principal", lambda: Principal(1, "u", "free", None))
    with pytest.raises(ToolError):
        await _call("store_memory", {"content_hash": "nope"})
    assert stats.errors == [("store_memory", "invalid_hash")]


async def test_mcp_sign_in_required_is_counted(stats):
    with pytest.raises(ToolError) as exc:
        await _call("store_memory", {"content_hash": HASH})
    assert '"authentication_required"' in str(exc.value)
    assert stats.errors == [("store_memory", "authentication_required")]


async def test_mcp_bug_is_internal_error_not_a_traceback(stats, monkeypatch, caplog):
    monkeypatch.setattr(mcp_app, "_principal", lambda: Principal(1, "u", "free", None))
    monkeypatch.setattr(mcp_app, "_adapter", BrokenAdapter())
    with pytest.raises(ToolError) as exc:
        await _call("store_memory", {"content_hash": HASH})
    text = str(exc.value)
    assert '"internal_error"' in text and "nobody anticipated" not in text
    assert stats.errors == [("store_memory", "internal_error")]
    assert "nobody anticipated" in caplog.text  # the traceback is ours, in the log


@pytest.fixture
def rest(monkeypatch):
    s = FakeStats()
    main.app.state.stats = s
    main.app.dependency_overrides[current_principal] = lambda: Principal(1, "u", "free", None)
    main.app.dependency_overrides[main.get_adapter] = lambda: BrokenAdapter()
    main.app.dependency_overrides[main.get_ratelimiter] = lambda: Limiter()
    yield TestClient(main.app, raise_server_exceptions=False), s
    main.app.dependency_overrides.clear()
    del main.app.state.stats


def test_rest_refusals_and_bugs_are_counted_per_route(rest):
    client, s = rest
    r = client.post("/nvs/mem", json={"content_hash": "nope"})
    assert r.status_code == 400 and r.json()["detail"]["error"] == "invalid_hash"
    r = client.post("/nvs/mem", json={})
    assert r.status_code == 422
    r = client.post("/nvs/mem", json={"content_hash": HASH})
    assert r.status_code == 500 and r.json()["detail"]["error"] == "internal_error"
    assert "nobody anticipated" not in json.dumps(r.json())
    assert s.errors == [
        ("POST /nvs/mem", "invalid_hash"),
        ("POST /nvs/mem", "invalid_request"),
        ("POST /nvs/mem", "internal_error"),
    ]


def test_stray_paths_are_not_counted(rest):
    client, s = rest
    assert client.get("/wp-login.php").status_code == 404
    assert s.errors == []
