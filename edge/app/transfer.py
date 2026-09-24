"""Handing names over: from the gateway's wallet to an address the agent names.

Until now every record was held by the gateway, with the agent's claim to it
asserted only inside the value. A transfer makes the holding real — and final:
the gateway cannot change, renew or take back a name once it has left.

Any valid address is accepted. With a key the agent holds, it can sign and prove
control, and must run its own node to change the record later. With an address
nobody holds a key to, the record is sealed: no one can change it until its term
ends. Just dating something needs no transfer at all — a record on the gateway's
address is dated too.

Policy lives here; the adapter only moves names. Shared by REST and MCP.
"""
from __future__ import annotations

from .auth import Principal
from .client import AdapterClient, AdapterError
from .config import settings
from .errors import AgentError
from .names import owned_by, root_name
from .ratelimit import RateLimiter
from .records import RecordLister

MAX_NAMES = 100

AFTER = (
    "The transfer is final once its block confirms. The gateway can no longer change, "
    "renew or return these names; each carries about a century of term from now. If you "
    "hold the key to the destination, you alone can update them — with your own Emercoin "
    "node, paying its fees. If no one holds that key, they are sealed until the term ends. "
    "New memories you store are held by the gateway again, and can be transferred later."
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
    quota = await ratelimiter.admit_write(principal, len(names))
    res = await adapter.transfer(names, to_address, settings.transfer_days)
    return {
        "txid": res["txid"],
        "count": res["count"],
        "names": res["names"],
        "to_address": res["toaddress"],
        "after": AFTER,
        "quota": quota,
    }
