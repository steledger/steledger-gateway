"""Async JSON-RPC client for an Emercoin Core node.

The node has no real auth (rpcuser/rpcpassword over the internal docker network);
all node access is mediated by this adapter, which is the only thing that speaks
RPC. Everything above the adapter talks plain HTTP REST.
"""
from __future__ import annotations

from typing import Any

import httpx


class RPCError(Exception):
    """A non-null `error` object came back from the node."""

    def __init__(self, code: int | None, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"RPC error {code}: {message}")


class EmercoinRPC:
    def __init__(self, url: str, user: str, password: str, timeout: float = 30.0) -> None:
        self._url = url
        self._client = httpx.AsyncClient(
            auth=(user, password),
            timeout=timeout,
            headers={"content-type": "text/plain"},
        )

    async def call(self, method: str, *params: Any) -> Any:
        payload = {"jsonrpc": "1.0", "id": "adapter", "method": method, "params": list(params)}
        try:
            resp = await self._client.post(self._url, json=payload)
        except httpx.TransportError as exc:
            # The node is down or restarting: say so in words the edge recognises,
            # instead of letting it surface as an unexplained 500.
            raise RPCError(None, f"node unreachable: {exc.__class__.__name__}") from exc
        # Emercoin/Bitcoin RPC returns HTTP 500/404 with a JSON-RPC error body for
        # method-level failures, so parse the body before raising on HTTP status.
        try:
            data = resp.json()
        except ValueError:
            resp.raise_for_status()
            raise
        if data.get("error"):
            err = data["error"]
            raise RPCError(err.get("code"), err.get("message", "unknown"))
        resp.raise_for_status()
        return data["result"]

    async def aclose(self) -> None:
        await self._client.aclose()
