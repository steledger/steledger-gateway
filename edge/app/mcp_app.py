"""Remote MCP server (Streamable HTTP), mounted into the edge.

Exposes the edge's chain operations as MCP tools so an MCP client (Claude, etc.)
can use the Emercoin chain as an identity + memory layer without a local install.
Stateless HTTP with JSON responses.

Auth: OAuth 2.1 (DCR + authorization-code + PKCE + refresh) via `oauth_provider`,
delegating user login to GitHub. Discovery and read tools (`node_status`,
`read_record`, `whoami`) are open; write tools require a valid token. The issued
access token is our session JWT, so a token pasted from /login works as a Bearer
too. The authenticated caller is read from the SDK auth context; the User-Agent
(for stats) comes from the request Context. Tools carry parameter descriptions,
output schemas and behaviour annotations. Shared clients via `configure()`.
"""
from __future__ import annotations

import functools
import inspect
import json
import logging
from typing import Annotated, TypedDict

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.middleware.bearer_auth import RequireAuthMiddleware
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import AnyHttpUrl, Field
from starlette.applications import Starlette
from starlette.routing import Route

from . import names, transfer
from .auth import Principal
from .client import AdapterClient
from .config import settings
from .github import GitHubOAuth
from .oauth_provider import MCP_SCOPE, GitHubOAuthProvider
from .ratelimit import RateLimiter
from .client import AdapterError
from .errors import AgentError, from_adapter
from .records import RecordLister, page
from .stats import Stats

log = logging.getLogger("edge.mcp")

_adapter: AdapterClient | None = None
_ratelimiter: RateLimiter | None = None
_stats: Stats | None = None
_records: RecordLister | None = None

oauth_provider = GitHubOAuthProvider()


def configure(
    adapter: AdapterClient, ratelimiter: RateLimiter, stats: Stats, github: GitHubOAuth,
    records: RecordLister,
) -> None:
    """Inject the edge's shared clients so tools/provider reuse them (in lifespan)."""
    global _adapter, _ratelimiter, _stats, _records
    _adapter, _ratelimiter, _stats, _records = adapter, ratelimiter, stats, records
    oauth_provider.configure(github, settings.redis_url)


def _principal_optional() -> Principal | None:
    """The authenticated caller if a valid token was presented, else None.

    Read/discovery tools are open (no transport-level auth gate — see
    `streamable_app`); they tolerate None. Write tools require auth and call
    `_principal()` instead."""
    at = get_access_token()
    if at is None:
        return None
    claims = at.claims or {}
    return Principal(
        int(at.subject), claims.get("login", ""), claims.get("tariff", "free"), claims.get("ghc")
    )


# Actionable, machine-readable payload returned (as the tool error text) when a
# write tool is called without a session. JSON so an agent can parse the reason and
# the remedy rather than scrape prose; `isError` stays true so it is never mistaken
# for a successful write.
_AUTH_REQUIRED = {
    "error": "authentication_required",
    "message": "This tool writes to the Emercoin chain under your identity, so it needs a signed-in session.",
    "how_to_fix": (
        "Connect over OAuth — your MCP client signs in with GitHub automatically "
        "(the server advertises the flow at /.well-known/oauth-protected-resource). "
        "Once the session carries a Bearer token, retry this call."
    ),
    "open_without_auth": ["node_status", "read_record", "list_records", "whoami"],
    # Derived, not hard-coded: this URL is handed to agents, and a second copy of
    # the hostname is a second thing to forget when the host moves.
    "docs": f"{settings.public_url.rstrip('/')}/docs/mcp.md",
}


def _principal() -> Principal:
    """The authenticated caller; raises a structured auth-required error if no valid
    token. Write tools require this (the error rides as `isError: true` tool text)."""
    p = _principal_optional()
    if p is None:
        raise ValueError(json.dumps(_AUTH_REQUIRED))
    return p


async def _record(ctx: Context, tool: str, principal: Principal | None) -> None:
    ua = ""
    try:
        ua = ctx.request_context.request.headers.get("user-agent", "")
    except Exception:  # noqa: BLE001
        pass
    if _stats is not None:
        await _stats.record_call(tool, principal.github_id if principal else None, ua)


# --- output schemas (drive each tool's outputSchema) -----------------------

class NodeStatus(TypedDict, total=False):
    """Node sync status. Fields are nullable — the MCP SDK fills any absent field
    with null when serialising structured output, so the schema must allow it."""
    version: str | None
    blocks: int | None
    headers: int | None
    verificationprogress: float | None
    connections: int | None
    synced: bool | None


class NvsRecord(TypedDict, total=False):
    """An NVS record (confirmed from the name DB, or pending from the mempool).
    Fields are nullable: a pending record omits several, and the SDK serialises
    absent fields as null. `status` says whether the write landed; `expired` says
    whether the name is still held — a lapsed record still reads back as
    'confirmed'."""
    status: str | None
    name: str | None
    value: str | None
    txid: str | None
    time: int | None
    address: str | None
    address_is_mine: str | None
    operation: str | None
    days_added: int | None
    expired: bool | None
    expires_in: int | None
    expires_at: int | None
    pending_update: bool | None
    pending: dict | None


class Quota(TypedDict, total=False):
    """Writes this account has left on its tier. `writes_open_on` (a UTC date)
    appears only while the GitHub account is too new to write."""
    writes_left_this_minute: int
    writes_left_today: int
    writes_open_on: str | None


class WhoAmI(TypedDict, total=False):
    """The current session's identity. `authenticated` is always present; the
    GitHub-rooted fields are filled only when signed in, and `hint` only when not
    (nullable so the SDK may serialise the absent ones as null)."""
    authenticated: bool
    github_id: int | None
    github_login: str | None
    tariff: str | None
    quota: Quota | None
    hint: str | None


class RecordEntry(TypedDict, total=False):
    """One record under a GitHub id. `kind` is identity, memory or other;
    `content_hash` is set for memories, `address` for the identity record."""
    name: str
    kind: str
    content_hash: str | None
    address: str | None
    metadata: dict | str | None
    registered_at: int | None
    expires_in: int | None
    expired: bool


class RecordList(TypedDict):
    """A page of records, newest first. Pass `next_offset` back as `offset` for
    the next page; it is null on the last one."""
    github_id: int
    total: int
    offset: int
    next_offset: int | None
    records: list[RecordEntry]


class WriteResult(TypedDict):
    """The on-chain write: the NVS name written, its transaction id, and the
    writes this account has left afterwards."""
    name: str
    txid: str
    quota: Quota


class BatchWriteResult(TypedDict):
    """One transaction for the whole batch: its id, every name written, and the
    writes this account has left afterwards."""
    txid: str
    count: int
    names: list[str]
    quota: Quota


class TransferResult(TypedDict):
    """One transaction for the whole transfer: its id, the names moved, where
    they went, what that means from now on, and the writes left afterwards."""
    txid: str
    count: int
    names: list[str]
    to_address: str
    after: str
    quota: Quota


class MemoryItem(TypedDict, total=False):
    content_hash: str
    metadata: dict | None


mcp = FastMCP(
    "steledger",
    instructions=(
        "Give an AI agent a durable identity and a place to anchor what it knows, "
        "as records on a public blockchain that no single vendor owns or can switch "
        "off. Read tools (node_status, read_record, list_records, whoami) are open to everyone — no "
        "sign-in. Write tools (register_identity, store_memory, store_memory_batch, "
        "transfer_records) require a GitHub "
        "sign-in via OAuth, which your MCP client performs, from a GitHub account at "
        "least 30 days old; on the FREE tier writes are limited per minute and per "
        "day. Typical flow: whoami → register_identity(address) "
        "→ store_memory(hash) → read_record(name); in a later session, list_records "
        "finds what you anchored before. A write reads back as `pending` and "
        "becomes `confirmed` after the next block (about 8 minutes on average lately). The substrate is Emercoin, "
        "running since 2013 — named so that any record here can also be checked "
        "independently in a public block explorer, without trusting this service."
    ),
    stateless_http=True,
    json_response=True,
    streamable_http_path="/mcp",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    auth_server_provider=oauth_provider,
    auth=AuthSettings(
        issuer_url=AnyHttpUrl(settings.public_url),
        client_registration_options=ClientRegistrationOptions(
            enabled=True, valid_scopes=[MCP_SCOPE], default_scopes=[MCP_SCOPE]
        ),
        required_scopes=[MCP_SCOPE],
        # Advertise this server as its own resource (RFC 9728) so modern clients
        # discover the AS via /.well-known/oauth-protected-resource + the 401's
        # resource_metadata pointer. issuer == resource (combined AS+RS).
        resource_server_url=AnyHttpUrl(settings.public_url),
    ),
)


def _tool(**kwargs):
    """Register an MCP tool with a cleaned-up description.

    `mcp.tool` takes the description straight from `__doc__`, source indentation
    and all, and that text is what an agent reads to decide how to call the tool.
    mcp ran docstrings through `cleandoc` up to 1.12; current 1.x does not, so do
    it here — once, for every tool.
    """
    register = mcp.tool(**kwargs)

    def decorate(fn):
        if fn.__doc__:
            fn.__doc__ = inspect.cleandoc(fn.__doc__)

        # Refusals reach the agent as the same JSON object the REST API sends
        # (see errors.py), not as "Error executing tool: 429: ..." prose.
        @functools.wraps(fn)
        async def wrapped(*args, **kwargs):
            try:
                return await fn(*args, **kwargs)
            except AdapterError as exc:
                raise ValueError(json.dumps(from_adapter(exc).detail)) from exc
            except AgentError as exc:
                raise ValueError(json.dumps(exc.detail)) from exc

        return register(wrapped)

    return decorate


@_tool(
    title="Node status",
    annotations=ToolAnnotations(
        title="Node status", readOnlyHint=True, idempotentHint=True, openWorldHint=True
    ),
    structured_output=True,
)
async def node_status(ctx: Context) -> NodeStatus:
    """Check that the chain node behind this service is healthy and fully synced:
    its version, block height, header height, peer connections and sync state.
    It is an Emercoin node. Read-only, no sign-in required, no parameters.

    `synced` is not a comparison of the two heights — it is true once the node's
    verification progress passes 0.9999, so it can still be false while `blocks`
    and `headers` already match. Trust that field, not the arithmetic. While it is
    false, a `read_record` may reflect an older state of the chain; writes still
    work, they simply confirm later.

    Also the way to make sense of expiry: `expires_in` on a record is denominated
    in blocks, and this tool reports the current height, so the two together are
    the only authoritative answer to when something lapses. A term is bought in
    days and charged at a flat 175 blocks each; the chain has been producing about
    171 a day lately (8.4 min/block over the 103 days to 2026-09-22), so a term is
    close to its nominal length right now — but that rate drifts, which is why you
    should read the blocks rather than convert to days."""
    await _record(ctx, "node_status", _principal_optional())
    return await _adapter.status()  # type: ignore[return-value]


@_tool(
    title="Read NVS record",
    annotations=ToolAnnotations(
        title="Read NVS record", readOnlyHint=True, idempotentHint=True, openWorldHint=True
    ),
    structured_output=True,
)
async def read_record(
    ctx: Context,
    name: Annotated[
        str,
        Field(description=(
            "Full NVS record name to read. Identity records are 'ai:gh:<github_id>' "
            "(e.g. 'ai:gh:3772563'); memory records are "
            "'ai:gh:<github_id>:mem:<sha256-hex>'. Any existing NVS name works."
        )),
    ],
) -> NvsRecord:
    """Read one on-chain record by its full name — an
    agent's identity (`ai:gh:<github_id>`) or a memory
    (`ai:gh:<github_id>:mem:<hash>`) written by `register_identity` / `store_memory`.
    Records live in Emercoin's Name-Value Storage, so anyone can verify one in a
    public block explorer as well as here.
    Returns the confirmed on-chain record, or a `pending` one still in the mempool —
    the `status` field ('confirmed' | 'pending') distinguishes them. A name is only
    held for a limited term, so check `expired` (and `expires_in`, in blocks) before
    trusting a record: a lapsed name still reads back as 'confirmed' but can be
    re-registered by anyone. Read-only, no sign-in required; use `whoami` to find
    your own github_id. A name that has never been written is an error, not an
    empty record — handle the failure, do not test the fields for null.
    `name` is the full NVS name and is capped at 512 bytes by the chain."""
    await _record(ctx, "read_record", _principal_optional())
    return await _adapter.read(name)  # type: ignore[return-value]


@_tool(
    title="List records",
    annotations=ToolAnnotations(
        title="List records", readOnlyHint=True, idempotentHint=True, openWorldHint=True
    ),
    structured_output=True,
)
async def list_records(
    ctx: Context,
    github_id: Annotated[
        int | None,
        Field(default=None, description=(
            "Numeric GitHub id whose records to list. Omit it to list your own "
            "(needs a signed-in session; `whoami` shows the id)."
        )),
    ] = None,
    limit: Annotated[
        int, Field(default=50, ge=1, le=200, description="Records per page, 1–200.")
    ] = 50,
    offset: Annotated[
        int, Field(default=0, ge=0, description="Where the page starts; use `next_offset` from the previous page.")
    ] = 0,
) -> RecordList:
    """List every record under one GitHub id — its identity record `ai:gh:<id>` and
    all its memories `ai:gh:<id>:mem:<hash>` — newest first, with each memory's
    content hash and metadata. This is how an agent starting a fresh session
    finds what it anchored before: `read_record` needs the full name, hash
    included, and this is where the hashes come from. Read-only, no sign-in
    needed to list any id; omit `github_id` to list your own when signed in.

    What comes back is the chain's view: confirmed records only (a write still in
    the mempool shows up after its block), expired ones included and flagged
    `expired` — their names can be taken by someone else, so do not treat them
    as yours. Only fingerprints and metadata live here; the content itself stays
    wherever you stored it. Results are cached for about a minute, so a record
    confirmed seconds ago may take that long to appear."""
    p = _principal_optional()
    await _record(ctx, "list_records", p)
    if github_id is None:
        if p is None:
            raise ValueError(json.dumps({
                "error": "github_id_required",
                "message": "No github_id given, and no signed-in session to take it from.",
                "how_to_fix": "Pass github_id, or sign in (GitHub OAuth) to list your own records.",
            }))
        github_id = p.github_id
    return page(await _records.list(github_id), github_id, limit, offset)  # type: ignore[union-attr,return-value]


@_tool(
    title="Who am I",
    annotations=ToolAnnotations(
        title="Who am I", readOnlyHint=True, idempotentHint=True, openWorldHint=False
    ),
    structured_output=True,
)
async def whoami(ctx: Context) -> WhoAmI:
    """Report the current session's identity. Read-only, no sign-in required: an
    anonymous session gets `{authenticated: false}` with a hint (not an error),
    a signed-in one gets `{authenticated: true}` plus the GitHub-rooted id, login
    and tariff. Call it to confirm who you are before `register_identity` /
    `store_memory`; an anonymous caller must sign in (GitHub OAuth) first.

    `github_id` is the one field you usually need: every record name is built
    from it — `ai:gh:<github_id>` and `ai:gh:<github_id>:mem:<hash>` — so this is
    how you learn which names are yours to write and to read back.

    `tariff` is `free` for every account today; it governs the write limits,
    currently 10 writes per minute and 100 per trailing 24 hours per account, and
    writing needs a GitHub account at least 30 days old. `quota` says how many
    writes are left right now (and, for a young account, the date writes open);
    every write returns the same figures, so plan batches with them. Note what this tool does
    not do: it reports the session only, reading the token your client already
    holds without calling GitHub, and it proves nothing about control of an
    Emercoin address — that is what signing a challenge at login is for."""
    p = _principal_optional()
    await _record(ctx, "whoami", p)
    if p is None:
        return {
            "authenticated": False,
            "hint": (
                "Anonymous session. Sign in with GitHub via your MCP client's OAuth flow "
                "to get an identity and use the write tools; read tools work without it."
            ),
        }
    return {
        "authenticated": True,
        "github_id": p.github_id,
        "github_login": p.github_login,
        "tariff": p.tariff,
        "quota": await _ratelimiter.remaining(p),  # type: ignore[union-attr]
    }


@_tool(
    title="Register identity",
    annotations=ToolAnnotations(
        title="Register identity",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
    structured_output=True,
)
async def register_identity(
    ctx: Context,
    address: Annotated[
        str,
        Field(description=(
            "Emercoin address to bind to your GitHub identity, e.g. 'EVfAn...'. It is "
            "the anchor for later signature login — you must control its key (control "
            "is proven when you sign a challenge at login, not here)."
        )),
    ],
    metadata: Annotated[
        dict | None,
        Field(default=None, description=(
            "Optional JSON object stored verbatim in the identity record, "
            "e.g. {\"agent\": \"my-bot\", \"url\": \"https://...\"}. Omit if unused."
        )),
    ] = None,
) -> WriteResult:
    """Create or rotate your on-chain identity record `ai:gh:<github_id>`, binding an
    Emercoin address to your GitHub identity. Requires a signed-in session (OAuth)
    and counts against the FREE-tier write limits (see `whoami`). Run `whoami` first to
    confirm you are signed in; anchor memories under this identity afterwards with
    `store_memory`. Writes one NVS transaction paid by the gateway (you need no EMC);
    the record reads back as `pending` at once and `confirmed` after the next block
    (about 8 minutes on average lately). Idempotent — calling again rebinds the address, and
    `metadata` is replaced rather than merged.

    Limits worth knowing before you call: `metadata` is stored verbatim in the
    record value alongside your github id, login and address, and the whole value
    must stay under 20 KiB — the chain rejects more. Every 128 bytes of name plus
    value adds about 0.0001 EMC to the fee the gateway pays for you. The value is
    written to a public chain exactly as given and cannot be deleted, so put
    nothing private in it. `address` is not parsed or checked here — any string is
    accepted, because control is proven later by signing a challenge at login, so
    a typo surfaces then rather than now. Returns the record name and the
    transaction id."""
    p = _principal()
    await _record(ctx, "register_identity", p)
    quota = await _ratelimiter.admit_write(p)
    name = names.root_name(p.github_id)
    value = {
        "github_id": p.github_id,
        "github_login": p.github_login,
        "address": address,
        "metadata": metadata or {},
    }
    res = await _adapter.write(name, value, settings.nvs_default_days)
    return {"name": res["name"], "txid": res["result"], "quota": quota}


@_tool(
    title="Store memory",
    annotations=ToolAnnotations(
        title="Store memory",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    ),
    structured_output=True,
)
async def store_memory(
    ctx: Context,
    content_hash: Annotated[
        str,
        Field(description=(
            "Hash of the artifact/memory, e.g. a SHA-256 hex digest. It becomes the "
            "record's ':mem:<hash>' suffix; the content itself stays off-chain "
            "(e.g. IPFS) — only this fingerprint is anchored."
        )),
    ],
    metadata: Annotated[
        dict | None,
        Field(default=None, description=(
            "Optional JSON object stored with the record (note, source, tags, …). "
            "Omit if unused."
        )),
    ] = None,
) -> WriteResult:
    """Anchor a fingerprint of a memory or artifact on-chain as the NVS record
    `ai:gh:<github_id>:mem:<content_hash>`. Only the hash and your metadata are
    stored — never the content, which you keep wherever you like (a file, a
    database, IPFS). What you get is a tamper-evident, timestamped proof that
    content with this hash existed, which anyone can verify later; `list_records`
    finds your earlier ones again. Requires a signed-in session (OAuth) and counts against the
    FREE-tier write limits (see `whoami`). Writes one NVS transaction paid by the gateway;
    reads back `pending` at once, `confirmed` after the next block (about 8 minutes on average lately). Not
    idempotent — each distinct hash is a new record. Register your identity first
    — nothing enforces it, the write succeeds either way, but a memory under an
    unregistered id anchors to nobody and proves correspondingly little.

    Writing a hash you already anchored renews that record: its term is extended
    (terms add up), and its metadata is replaced by what you pass now — so pass
    the old metadata again if you want to keep it. That is the only renewal there
    is; a record left alone lapses after its term (see `expires_in`).

    Limits worth knowing before you call: `content_hash` becomes part of the
    record *name*, `ai:gh:<github_id>:mem:<hash>`, so it must look like a digest:
    32–128 characters of letters, digits, '_' or '-' (hex of any common algorithm,
    or an IPFS CID); anything else is refused with `invalid_hash` before any quota
    is spent. Beyond that shape it is never verified: nothing checks that it is
    the hash of anything, so a wrong digest anchors happily and proves nothing. `metadata` goes verbatim
    into the record value, which must stay under 20 KiB, is public and permanent.
    Returns the record name and the transaction id."""
    p = _principal()
    await _record(ctx, "store_memory", p)
    name = names.mem_name(p.github_id, content_hash)  # validates before spending quota
    quota = await _ratelimiter.admit_write(p)
    value = {"github_id": p.github_id, "content_hash": content_hash, "metadata": metadata or {}}
    res = await _adapter.write(name, value, settings.nvs_default_days)
    return {"name": res["name"], "txid": res["result"], "quota": quota}


@_tool(
    title="Store memories (batch)",
    annotations=ToolAnnotations(
        title="Store memories (batch)",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    ),
    structured_output=True,
)
async def store_memory_batch(
    ctx: Context,
    records: Annotated[
        list[MemoryItem],
        Field(min_length=1, max_length=100, description=(
            "1–100 items, each {\"content_hash\": \"<digest>\", \"metadata\": {...}}; "
            "metadata is optional. Same rules as store_memory for each item."
        )),
    ],
) -> BatchWriteResult:
    """Anchor many fingerprints in ONE on-chain transaction — the way to record a
    session's worth of artifacts without spending a write per minute on each.
    Each item becomes `ai:gh:<github_id>:mem:<content_hash>`, exactly as with
    `store_memory`: only hashes and metadata, never content. All or nothing: if
    any item is refused (a malformed hash, say), nothing is written.

    Requires a signed-in session. A batch of N counts as N writes against the
    FREE-tier limits (10 per minute, 100 per 24 hours), so at most 10 items fit
    in one call on a fresh minute; the result says how many writes are left.
    One transaction id comes back for the whole batch; each name reads back
    `pending` at once and `confirmed` after the next block."""
    p = _principal()
    await _record(ctx, "store_memory_batch", p)
    items = [(names.mem_name(p.github_id, r["content_hash"]), r) for r in records]
    quota = await _ratelimiter.admit_write(p, len(items))  # type: ignore[union-attr]
    ops = [
        {
            "name": name,
            "value": {"github_id": p.github_id, "content_hash": r["content_hash"],
                      "metadata": r.get("metadata") or {}},
            "days": settings.nvs_default_days,
        }
        for name, r in items
    ]
    res = await _adapter.write_batch(ops)  # type: ignore[union-attr]
    return {"txid": res["txid"], "count": res["count"], "names": res["names"], "quota": quota}


@_tool(
    title="Transfer records",
    annotations=ToolAnnotations(
        title="Transfer records",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    ),
    structured_output=True,
)
async def transfer_records(
    ctx: Context,
    to_address: Annotated[
        str,
        Field(description=(
            "Emercoin address that will hold the records from now on. Any valid address "
            "is accepted: one whose key you hold, or one no one holds a key to, which "
            "seals the records."
        )),
    ],
    irreversible: Annotated[
        bool,
        Field(description=(
            "Must be true. Confirms you understand the gateway can never change, renew "
            "or return these records afterwards."
        )),
    ],
    names: Annotated[
        list[str] | None,
        Field(default=None, max_length=100, description=(
            "Records to transfer, e.g. [\"ai:gh:123\", \"ai:gh:123:mem:<hash>\"] — "
            "only your own. Omit and set `everything` instead to move them all."
        )),
    ] = None,
    everything: Annotated[
        bool,
        Field(default=False, description=(
            "Transfer your identity and every live memory at once (up to 100). "
            "Use instead of `names`."
        )),
    ] = False,
) -> TransferResult:
    """Hand your records over from the gateway's wallet to an address you choose,
    in one transaction. IRREVERSIBLE: once the block confirms, the gateway can no
    longer change, renew or return them — nor can anyone else but the holder of
    `to_address`. Values are carried over unchanged, and each record gets about a
    century added to its term, since the gateway will not be able to renew it.

    Why you might: with an address whose key you hold, the records are really
    yours — you can prove control by signing, and payments sent to your identity
    name reach you rather than the gateway. Changing a record afterwards needs your
    own Emercoin node and its fees. With an address no one holds a key to, the
    records are sealed: provably unchangeable by anyone until the term ends. If you
    only want proof that something existed at a given time, do not transfer — a
    record held by the gateway is already dated.

    Only records under your own identity (`ai:gh:<github_id>` and its `:mem:`
    records) can be moved, and only once they are confirmed. Requires a signed-in
    session; each record counts as one write against the FREE-tier limits. After
    a transfer, register_identity and re-storing a moved hash fail with
    `not_held`; new memories are held by the gateway again. Returns the
    transaction id, the names moved, and a plain statement of what changes."""
    p = _principal()
    await _record(ctx, "transfer_records", p)
    return await transfer.transfer(
        p, to_address, names, everything, irreversible, _adapter, _ratelimiter, _records  # type: ignore[arg-type]
    )


def streamable_app() -> Starlette:
    """The MCP streamable-HTTP app with the transport-level auth gate removed.

    By default the SDK wraps the `/mcp` route in `RequireAuthMiddleware`, which 401s
    every unauthenticated request — including the `initialize`/`tools/list` handshake.
    That blocks open discovery and read access (and makes registry health-checks like
    Glama's headless prober report the connector as Unhealthy, since they can't
    complete an interactive OAuth flow).

    We unwrap that one middleware so anonymous callers reach the transport. The
    app-level `AuthenticationMiddleware` + `AuthContextMiddleware` stay, so a Bearer
    token is still validated and exposed via `get_access_token()` when present — which
    is how the write tools enforce auth per-call through `_principal()`. OAuth routes
    and protected-resource metadata are untouched."""
    app = mcp.streamable_http_app()
    for route in app.routes:
        if (
            isinstance(route, Route)
            and route.path == mcp.settings.streamable_http_path
            and isinstance(route.app, RequireAuthMiddleware)
        ):
            route.app = route.app.app  # drop the 401-for-anonymous gate
    return app
