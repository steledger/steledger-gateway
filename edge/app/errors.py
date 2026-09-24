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

import json
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
        ("is not yours",),
        409, "not_held",
        "This gateway no longer holds that name: it was transferred to another address, "
        "and only the holder of that address can change or renew it now.",
        "read_record shows the name's current `address`. If it is yours, update the record "
        "with your own node; this service cannot.",
        None,
    ),
    (
        ("not a valid emercoin address",),
        400, "invalid_address",
        "The destination is not a valid Emercoin address.",
        "Check it for typos. Any valid address is accepted, including one no one holds a key to.",
        None,
    ),
    (
        ("the name is not active",),
        409, "not_active",
        "That record's term is over, so there is nothing to transfer.",
        "Write it again first (store_memory or register_identity), wait for the block, then transfer.",
        None,
    ),
    (
        ("node busy",),
        503, "busy",
        "Too many reads are in flight at once; this one was turned away rather than queued.",
        "Retry in a few seconds.",
        5,
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


# The answer to anything unexpected: the agent gets a code it can act on, never
# a traceback; we get the traceback in the log and a count in the stats.
INTERNAL_ERROR = {
    "error": "internal_error",
    "message": "Something failed on our side. It has been logged and counted.",
    "how_to_fix": "Retry once. If it keeps failing, it is our bug: "
                  "https://github.com/steledger/steledger-gateway/issues",
}


def code_of(exc: Exception) -> str:
    """The stable code carried by a refusal already rendered as JSON text."""
    try:
        return str(json.loads(str(exc))["error"])
    except (ValueError, KeyError, TypeError):
        return "invalid_input"


def not_held(name: str, address: str | None) -> AgentError:
    """A write to a name that was transferred away — refused before any quota."""
    return AgentError(
        409, "not_held",
        f"This gateway no longer holds {name}: it was transferred to {address}, and only "
        "the holder of that address can change or renew it now.",
        "If that address is yours, update the record with your own Emercoin node; this "
        "service cannot. New memories under other hashes are unaffected.",
    )


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
