"""NVS (name-value storage) mechanics over the node RPC.

This layer is policy-free: it knows how to create / update / read NVS names and
nothing about who owns them. Naming conventions (e.g. the `ai:gh:<id>` namespace)
and authorization live above the adapter, in the edge service.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from .rpc import EmercoinRPC, RPCError


def _encode(value: Any) -> str:
    """A value may arrive as a dict (encode it) or an already-serialized string."""
    if isinstance(value, str):
        return value
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


async def _held(rpc: EmercoinRPC, name: str) -> bool:
    """True if name_show finds the name within its term.

    An expired name keeps showing up in name_show (with `expired: true`), but the
    node refuses name_update on it — "name_update on an inactive name". Its term
    is over, so the name is free to register again and only name_new works.
    """
    try:
        return not (await show_record(rpc, name)).get("expired")
    except RPCError:
        return False


async def name_is_active(rpc: EmercoinRPC, name: str) -> bool:
    """True if the name is registered AND still within its term (or sitting
    unconfirmed in the mempool). Determines name_new vs name_update."""
    return await _held(rpc, name) or await find_in_mempool(rpc, name) is not None


async def active_names(rpc: EmercoinRPC, names: list[str]) -> set[str]:
    """`name_is_active` for many names, reading the mempool once rather than per name."""
    try:
        pending = {e.get("name") for e in await rpc.call("name_mempool") if isinstance(e, dict)}
    except RPCError:
        pending = set()
    return {n for n in names if n in pending or await _held(rpc, n)}


# Arguments never taken from a caller. `valuetype` is the dangerous one: anything
# other than "", "hex" or "base64" is read by the node as a FILE PATH on its own
# host, and the file's contents are written on-chain, publicly and for good. It
# sits right after `toaddress` positionally, so every call below spells both out
# instead of letting a missing or extra argument shift into that slot.
TO_SELF = ""        # toaddress: a fresh key from the wallet's own keypool
VALUE_AS_TEXT = ""  # valuetype: the value is the string itself
VALUE_AS_BASE64 = "base64"


def _op(verb: str, name: str, value: str, days: int, toaddress: str = TO_SELF,
        valuetype: str = VALUE_AS_TEXT) -> dict[str, Any]:
    """One name_updatemany operation, with every optional field set explicitly."""
    return {verb: name, "value": value, "days": days,
            "toaddress": toaddress, "valuetype": valuetype}


async def write_record(rpc: EmercoinRPC, name: str, value: Any, days: int) -> Any:
    """Register or update an NVS name (both single-step: name value days).

    name_new fails on a name that is currently held, so a re-registration (e.g. key
    rotation: same name, new value) must go through name_update. Once the term has
    lapsed it is the other way round: only name_new can take the name back.
    """
    method = "name_update" if await name_is_active(rpc, name) else "name_new"
    return await rpc.call(method, name, _encode(value), days, TO_SELF, VALUE_AS_TEXT)


async def write_batch(rpc: EmercoinRPC, operations: list[dict[str, Any]]) -> Any:
    """Atomic multi-record write in a single transaction (name_updatemany).

    `operations` is a list of {name, value, days}; returns one txid for the whole
    batch. Each name gets NEW or UPDATE by the same rule as `write_record` — the
    node refuses NEW on a held name, and one refusal fails the whole batch. Note:
    raw JSON-RPC wants a native array here (not the string form shown in
    bitcoin-cli examples).
    """
    active = await active_names(rpc, [op["name"] for op in operations])
    ops = [
        _op("UPDATE" if op["name"] in active else "NEW", op["name"], _encode(op["value"]), op["days"])
        for op in operations
    ]
    return await rpc.call("name_updatemany", ops)


async def address_is_valid(rpc: EmercoinRPC, address: str) -> bool:
    """Whether the node accepts `address` as a destination. The node is the
    authority on address formats (base58 and bech32 alike), so none is parsed here."""
    return bool((await rpc.call("validateaddress", address)).get("isvalid"))


class TransferError(Exception):
    """A transfer the adapter refuses before asking the node."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


async def transfer(rpc: EmercoinRPC, names: list[str], toaddress: str, days: int) -> Any:
    """Move names this wallet holds to `toaddress`, in one transaction.

    Irreversible: once the transaction confirms, this wallet can neither change
    nor renew those names. Each name keeps its value byte for byte — it is read
    and re-sent as base64 — and gets `days` added to its remaining term. Any
    address the node accepts will do, including one nobody holds a key for,
    which seals the record until its term runs out.
    """
    if not await address_is_valid(rpc, toaddress):
        raise TransferError(400, f"{toaddress!r} is not a valid Emercoin address")
    ops = []
    for name in names:
        try:
            record = await rpc.call("name_show", name, VALUE_AS_BASE64)
        except RPCError as exc:
            raise TransferError(404, f"{name}: {exc.message}")
        if record.get("expired") or record.get("deleted"):
            raise TransferError(409, f"{name}: the name is not active, there is nothing to transfer")
        ops.append(_op("UPDATE", name, record["value"], days, toaddress, VALUE_AS_BASE64))
    return await rpc.call("name_updatemany", ops)


async def show_record(rpc: EmercoinRPC, name: str) -> dict[str, Any]:
    return await rpc.call("name_show", name)


async def show_history(rpc: EmercoinRPC, name: str) -> Any:
    """Full value history of a name (name_history)."""
    return await rpc.call("name_history", name)


async def names_for_address(rpc: EmercoinRPC, address: str) -> Any:
    """All names owned by an address (name_scan_address)."""
    return await rpc.call("name_scan_address", address)


# name_filter walks the node's whole name index on every call (about 0.65 s on the
# production node, whatever it matches), so at most two scans run at once; the
# rest queue here instead of tying up the node's RPC threads.
_FILTER_SLOTS = asyncio.Semaphore(2)


async def filter_names(rpc: EmercoinRPC, regex: str) -> list[dict[str, Any]]:
    """Every name matching `regex` (name_filter), values in full.

    Positional arguments are spelled out because the defaults are not ours to
    rely on: maxage 0 (all blocks), from 0, nb 0 (no cap), no stats, plain-string
    values, max-value-length 0 (do not truncate). Expired names are included and
    carry `expired: true`; live ones omit the field.
    """
    async with _FILTER_SLOTS:
        return await rpc.call("name_filter", regex, 0, 0, 0, "", "", 0)


async def find_in_mempool(rpc: EmercoinRPC, name: str) -> dict[str, Any] | None:
    """A just-written name lives in the mempool until a block confirms it;
    name_show can't see it yet, but name_mempool can."""
    try:
        entries = await rpc.call("name_mempool")
    except RPCError:
        return None
    for entry in entries:
        if isinstance(entry, dict) and entry.get("name") == name:
            return entry
    return None
