"""Handing names over: from the gateway's wallet to an address the agent names.

Until now every record was held by the gateway, with the agent's claim to it
asserted only inside the value. A transfer makes the holding real — and final:
the gateway cannot change, renew or take back a name once it has left.

Any valid address is accepted. With a key the agent holds, it can sign and prove
control, and must run its own node to change the record later. With an address
nobody holds a key to, the record is sealed: no one can change it until its term
ends. Just dating something needs no transfer at all — a record on the gateway's
address is dated too.

A transfer may also ask for network-fee funds (grant.py): a small amount of
EMC sent to the same address, so the new holder can pay for changing its
records. They go out right after the transfer is broadcast, as a transaction of
their own.

Policy lives here; the adapter only moves names. Shared by REST and MCP.
"""
from __future__ import annotations

import logging

from .auth import Principal
from .client import AdapterClient, AdapterError
from .config import settings
from .errors import AgentError, not_held
from .grant import FeeGrants
from .names import owned_by, root_name
from .ratelimit import RateLimiter
from .records import RecordLister

log = logging.getLogger(__name__)

MAX_NAMES = 100

AFTER = (
    "The transfer is final once its block confirms. The gateway can no longer change, "
    "renew or return these names; each carries about a century of term from now. If you "
    "hold the key to the destination, you alone can update them — with your own Emercoin "
    "node, paying its fees. If no one holds that key, they are sealed until the term ends. "
    "New memories you store are held by the gateway again, and can be transferred later."
)

GRANT_SENT = (
    " {amount} EMC for network fees went to the same address in a separate transaction; "
    "spend it from your own node on changing or renewing these records. It is given once "
    "per account."
)
GRANT_FAILED = (
    " The network-fee funds could not be sent; the transfer itself went through. "
    "Your one grant is still unused: tell the operator with send_feedback."
)


async def transfer(
    principal: Principal,
    to_address: str,
    names: list[str] | None,
    everything: bool,
    irreversible: bool,
    adapter: AdapterClient,
    ratelimiter: RateLimiter,
    records: RecordLister,
    fee_grant: bool = False,
    grants: FeeGrants | None = None,
) -> dict:
    if not irreversible:
        raise AgentError(
            400, "confirmation_required",
            "A transfer cannot be undone: the gateway will no longer be able to change, "
            "renew or return these names.",
            "Pass irreversible=true once you are sure of the destination address.",
        )
    if bool(names) == everything:
        raise AgentError(
            400, "invalid_selection",
            "Say which records to transfer: either a list of names, or everything=true.",
            "Pass `names` (from list_records) or set `everything` — not both, not neither.",
        )

    if everything:
        listed = await records.list(principal.github_id)
        names = [r["name"] for r in listed if not r["expired"]]
        if not names:
            raise AgentError(
                404, "nothing_to_transfer",
                "This account has no live records yet (a write still waiting for its block "
                "does not count).",
                "Write something first, wait for the block, then transfer.",
            )
    assert names is not None
    names = list(dict.fromkeys(names))  # the node refuses a name twice in one transaction

    if len(names) > MAX_NAMES:
        raise AgentError(
            400, "too_many_names",
            f"{len(names)} records, but one transfer carries at most {MAX_NAMES}.",
            "Pass them in several calls using `names`, at most 100 at a time.",
        )
    foreign = [n for n in names if not owned_by(principal.github_id, n)]
    if foreign:
        raise AgentError(
            403, "not_your_record",
            f"Only records under your own identity can be transferred; not {foreign[0]!r}.",
            f"Your records are {root_name(principal.github_id)} and "
            f"{root_name(principal.github_id)}:mem:<hash>; list_records shows them.",
        )

    # Everything checkable is checked before the quota, so a refusal costs
    # nothing; the adapter checks again. The address first — one call — then
    # each name, which is a cheap key lookup.
    if not await adapter.address_is_valid(to_address):
        raise AgentError(
            400, "invalid_address",
            f"{to_address[:80]!r} is not a valid Emercoin address.",
            "Check it for typos. Any valid address is accepted, including one no one holds a key to.",
        )
    for name in names:
        try:
            record = await adapter.read(name)
        except AdapterError as exc:
            if exc.status_code == 404:
                raise AgentError(
                    404, "not_found", f"{name} does not exist on the chain.",
                    "Check the name, hash included; list_records shows your records.",
                )
            raise
        if record.get("status") == "pending" or record.get("pending_update"):
            raise AgentError(
                409, "record_pending",
                f"{name} is still waiting for its block; only confirmed records can be transferred.",
                "Wait until read_record shows it `confirmed` (about 8 minutes), then retry.",
                600,
            )
        if record.get("expired"):
            raise AgentError(
                409, "not_active", f"{name}'s term is over, so there is nothing to transfer.",
                "Write it again first, wait for the block, then transfer.",
            )
    # The grant is reserved last among the refusals, so no other refusal can
    # strand it, and given back if the transfer does not go out.
    grant = await grants.reserve(principal.github_id) if fee_grant else None  # type: ignore[union-attr]
    try:
        quota = await ratelimiter.admit_write(principal, len(names))
        res = await adapter.transfer(names, to_address, settings.transfer_days)
    except BaseException:
        if grant:
            await grants.release(principal.github_id, grant)  # type: ignore[union-attr]
        raise
    result = {
        "txid": res["txid"],
        "count": res["count"],
        "names": res["names"],
        "to_address": res["toaddress"],
        "after": AFTER,
        "fee_grant": None,
        "quota": quota,
    }
    if grant:
        result["fee_grant"] = await _send_grant(principal.github_id, to_address, grant, adapter, grants)  # type: ignore[arg-type]
        result["after"] += (
            GRANT_SENT.format(amount=result["fee_grant"]["amount"]) if result["fee_grant"]["txid"]
            else GRANT_FAILED
        )
    return result


async def _send_grant(
    github_id: int, address: str, token: str, adapter: AdapterClient, grants: FeeGrants
) -> dict:
    """Pay the reserved grant. The transfer has already gone out, so a failure
    here must not turn it into an error: report it, and give the grant back."""
    amount = settings.fee_grant_emc
    try:
        res = await adapter.send(address, float(amount), f"network-fee funds for gh:{github_id}")
    except AdapterError as exc:
        log.error("fee grant not sent (github_id=%s, address=%s): %s", github_id, address, exc)
        await grants.release(github_id, token)
        return {"amount": str(amount), "txid": None}
    await grants.settle(github_id, address, res["txid"])
    log.info("fee grant sent (github_id=%s, address=%s, txid=%s)", github_id, address, res["txid"])
    return {"amount": str(amount), "txid": res["txid"]}


async def ensure_writable(adapter: AdapterClient, names: list[str]) -> None:
    """Refuse, before any quota is spent, a write to a name already transferred away.

    Without this the node refuses too, but only after the write was admitted —
    so the refusal would cost a write of quota for nothing."""
    for name in names:
        held = await adapter.holder(name)
        if held["state"] == "foreign":
            raise not_held(name, held.get("address"))
