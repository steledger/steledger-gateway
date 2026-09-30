"""send_feedback: open, bounded, and stored as data — never relayed."""
from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient
from mcp.server.fastmcp.exceptions import ToolError

from app import main, mcp_app
from app.errors import FEEDBACK_HINT, AgentError
from app.feedback import ALL_PER_DAY, PER_SENDER_PER_DAY, Inbox

REDIS = os.environ.get("EDGE_TEST_REDIS_URL")
needs_redis = pytest.mark.skipif(not REDIS, reason="EDGE_TEST_REDIS_URL not set")


def test_every_refusal_points_at_feedback():
    assert AgentError(400, "invalid_hash", "m", "f").detail["feedback"] == FEEDBACK_HINT
    assert "feedback" not in AgentError(429, "feedback_limit", "m", "f").detail


class FakeInbox:
    def __init__(self):
        self.got = []

    async def submit(self, message, error_code, tool, client, github_id, ip):
        self.got.append((message, error_code, tool, github_id, ip))
        return {"received": True, "id": "abc", "note": "n"}


async def test_mcp_tool_is_open_and_passes_the_sender_through(monkeypatch):
    inbox = FakeInbox()
    monkeypatch.setattr(mcp_app, "_inbox", inbox)
    result = await mcp_app.mcp.call_tool("send_feedback", {"message": "read_record says not_found", "error_code": "not_found"})
    structured = result[1] if isinstance(result, tuple) else result
    assert structured["received"] is True
    assert inbox.got[0][:2] == ("read_record says not_found", "not_found") and inbox.got[0][3] is None


async def test_mcp_tool_rejects_an_overlong_message(monkeypatch):
    monkeypatch.setattr(mcp_app, "_inbox", FakeInbox())
    with pytest.raises(ToolError):
        await mcp_app.mcp.call_tool("send_feedback", {"message": "x" * 1001})


def test_rest_route_uses_the_cloudflare_address():
    inbox = FakeInbox()
    main.app.state.inbox = inbox
    try:
        r = TestClient(main.app).post("/feedback", json={"message": "hi", "tool": "list_records"},
                                      headers={"cf-connecting-ip": "203.0.113.9"})
    finally:
        del main.app.state.inbox
    assert r.status_code == 200 and inbox.got[0][4] == "203.0.113.9"


@needs_redis
async def test_stored_limited_and_cleaned():
    import redis.asyncio as redis
    r = redis.from_url(REDIS, decode_responses=True)
    await r.flushdb()
    inbox = Inbox(REDIS)
    with pytest.raises(AgentError) as e:
        await inbox.submit("   ", None, None, "ua", None, "1.1.1.1")
    assert e.value.detail["error"] == "empty_feedback"
    await inbox.submit("tool broke", "NOT FOUND!", "read_record", "ua", 7, "1.1.1.1")
    stored = json.loads((await r.zrange("feedback:inbox", 0, -1))[0])
    assert stored["message"] == "tool broke" and stored["error_code"] is None  # junk label dropped
    assert stored["tool"] == "read_record" and stored["github_id"] == 7
    assert "1.1.1.1" not in json.dumps(stored)  # the address is not kept with the message
    for _ in range(PER_SENDER_PER_DAY - 1):
        await inbox.submit("again", None, None, "ua", None, "1.1.1.1")
    with pytest.raises(AgentError) as e:
        await inbox.submit("one too many", None, None, "ua", None, "1.1.1.1")
    assert e.value.detail["error"] == "feedback_limit"
    await inbox.submit("someone else", None, None, "ua", None, "2.2.2.2")  # others unaffected
    assert ALL_PER_DAY >= PER_SENDER_PER_DAY
    await inbox.aclose()
    await r.aclose()
