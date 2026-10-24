"""/mcp over HTTP, as a real client sees it, in both protocol eras.

Each check is a way the mcp 1.x → 2.x move could fail without failing to start:
DNS-rebinding protection refusing a public Host with 421, the transport's auth gate
coming back and 401-ing anonymous reads, or a refusal reaching the model as a bare
"Error executing tool <name>" instead of the JSON the agent acts on.
"""
from __future__ import annotations

import json

import httpx2
from mcp.client.client import Client
from mcp.client.streamable_http import streamable_http_client

from app import main, mcp_app

URL = "https://api.steledger.com/mcp"


def _transport():
    http = httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=main.app), base_url="https://api.steledger.com"
    )
    return streamable_http_client(URL, http_client=http)


async def test_both_eras_reach_the_tools_anonymously(monkeypatch):
    # The session manager runs once per process, so one lifespan covers both eras.
    async with main.app.router.lifespan_context(main.app):
        monkeypatch.setattr(mcp_app, "_stats", None)
        for mode, version in (("auto", "2026-07-28"), ("legacy", "2025-11-25")):
            async with Client(_transport(), mode=mode) as client:
                assert client.protocol_version == version
                assert client.server_info.name == "steledger"
                assert client.server_info.version == mcp_app.SERVER_VERSION

                tools = {t.name for t in (await client.list_tools()).tools}
                assert {"whoami", "read_record", "register_identity", "send_feedback"} <= tools

                who = await client.call_tool("whoami", {})
                assert not who.is_error
                assert who.structured_content["authenticated"] is False

                refused = await client.call_tool("register_identity", {"address": "Ex"})
                assert refused.is_error
                prefix, _, body = refused.content[0].text.partition(": ")
                assert prefix == "Error executing tool register_identity"
                assert json.loads(body)["error"] == "authentication_required"
