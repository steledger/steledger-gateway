"""Errors an agent can act on.

Every refusal the edge sends — over REST or as an MCP tool error — has the same
shape: a stable machine code, what happened, what to do about it, and when a
retry makes sense. An agent should never have to parse prose, and never see a
raw node message it cannot interpret.

    {"error": "daily_limit", "message": "...", "how_to_fix": "...", "retry_after": 3600}

Over REST the object is the response's `detail`; over MCP it is the tool
error's text, JSON-encoded, with `isError: true`.
"""
from __future__ import annotations

import logging

from fastapi import HTTPException

from .client import AdapterError

log = logging.getLogger(__name__)


class AgentError(HTTPException):
    """An HTTPException whose detail is the structured object above."""

    def __init__(
        self,
        status_code: int,
        error: str,
        message: str,
        how_to_fix: str,
        retry_after: int | None = None,
    ) -> None:
        detail = {"error": error, "message": message, "how_to_fix": how_to_fix}
        headers = None
        if retry_after is not None:
            detail["retry_after"] = retry_after
            headers = {"Retry-After": str(retry_after)}
        super().__init__(status_code=status_code, detail=detail, headers=headers)


# Known node failures on a write, matched on the RPC message (the node reports
# most of them under generic codes). Anything unmatched is passed through as
# `node_error` with the node's own words: hiding it would be worse.
_NODE_WRITE_ERRORS = [
    (
        ("pending operations", "already in mempool", "txn-mempool-conflict"),
        409, "record_pending",
        "An earlier write to this record is still waiting for a block.",
        "Wait for it to confirm (read_record shows `pending` until then), then retry.",
        600,
    ),
    (
        ("insufficient funds", "insufficient balance"),
        503, "service_funds",
        "The gateway's wallet cannot pay the network fee right now. Nothing is wrong on your side.",
        "Retry later; reads are unaffected.",
        3600,
    ),
    (
        ("too long", "too big", "too large", "exceeds"),
        400, "value_too_large",
        "The record is larger than the chain accepts.",
        "Keep metadata small: the whole record value must stay under 20 KiB, the name under 512 bytes.",
        None,
    ),
]


def from_adapter(exc: AdapterError) -> AgentError:
    """Translate an adapter failure into something an agent can act on."""
    text = str(exc.detail)
    lowered = text.lower()
    for needles, status, code, message, fix, retry in _NODE_WRITE_ERRORS:
        if any(n in lowered for n in needles):
            if code == "service_funds":
                log.warning("gateway wallet cannot pay fees: %s", text)
            return AgentError(status, code, message, fix, retry)
    if exc.status_code == 404:
        return AgentError(
            404, "not_found", "No record by that name exists on the chain.",
            "Check the name, hash included. list_records shows every record under "
            "a GitHub id; a write reads back as `pending` as soon as it is sent.",
        )
    if "unreachable" in lowered:
        return AgentError(
            503, "node_unavailable", "The chain node behind this service is not answering.",
            "Retry in a few minutes; node_status shows when it is back.", 300,
        )
    return AgentError(
        502, "node_error", f"The chain node refused the operation: {text}",
        "Retrying will not help if the input is the cause. If it looks like a fault on "
        "our side, report it to security@steledger.com or on GitHub.",
    )
