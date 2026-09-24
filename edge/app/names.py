"""NVS naming policy — the `ai:gh:<id>` namespace lives here, in the edge.

The adapter is policy-free; it's the edge that decides how an agent's identity and
memory records are named, so agents can't collide or overwrite each other:
  ai:gh:<github_id>            -> root identity record
  ai:gh:<github_id>:mem:<hash> -> a research/memory pointer
"""
from __future__ import annotations

import json
import re
from typing import Any

from .errors import AgentError

# What a content hash may look like: 32–128 characters of [A-Za-z0-9_-]. That
# admits a hex digest of any common algorithm (MD5 to SHA-512) and IPFS CIDs, and
# keeps out what does harm: short or empty strings that fingerprint nothing, and
# ':' or spaces, which would let a "hash" fake the structure of the name itself.
HASH = re.compile(r"^[A-Za-z0-9_-]{32,128}$")


def root_name(github_id: int) -> str:
    return f"ai:gh:{github_id}"


def mem_name(github_id: int, content_hash: str) -> str:
    if not HASH.match(content_hash):
        raise AgentError(
            400, "invalid_hash",
            f"{content_hash[:80]!r} does not look like a content hash.",
            "Pass the digest itself — e.g. the 64 hex characters of `sha256sum` — or an "
            "IPFS CID: 32–128 characters of letters, digits, '_' or '-'.",
        )
    return f"ai:gh:{github_id}:mem:{content_hash}"


def owned_by(github_id: int, name: str) -> bool:
    """True if `name` is in this account's namespace: its identity or a memory."""
    root = root_name(github_id)
    return name == root or name.startswith(root + ":mem:")


def parse_identity(record: dict[str, Any]) -> dict[str, Any]:
    """Parse the JSON value of an on-chain identity record; {} if not valid JSON."""
    try:
        return json.loads(record.get("value", "{}"))
    except (ValueError, TypeError):
        return {}
