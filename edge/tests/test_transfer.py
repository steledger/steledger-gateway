"""The edge's transfer policy: whose names, how many, and only when confirmed.

    cd edge && EDGE_DEV_LOGIN_ENABLED=true uv run --with-requirements requirements.txt \
        python -m unittest discover -s tests
"""
from __future__ import annotations

import asyncio
import unittest

from app import transfer
from app.auth import Principal
from app.client import AdapterError
from app.errors import AgentError

ME = Principal(github_id=7, github_login="me", tariff="free")
ADDR = "EdCh5nZ9QuTrPbkmZXGm4e5r3iPpHS8xKM"


class FakeAdapter:
    def __init__(self, records=None):
        self.calls = []
        self.records = records

    async def read(self, name):
        if self.records is None:
            return {"status": "confirmed"}
        if name not in self.records:
            raise AdapterError(404, "name not found")
        return self.records[name]

    async def address_is_valid(self, address):
        return address == ADDR

    async def transfer(self, names, toaddress, days):
        self.calls.append((names, toaddress, days))
        return {"txid": "tx", "count": len(names), "names": names, "toaddress": toaddress}


class FakeLimiter:
    def __init__(self):
        self.admitted = []

    async def admit_write(self, principal, n=1):
        self.admitted.append(n)
        return {"writes_left_today": 100 - n}


class FakeRecords:
    def __init__(self, listed):
        self.listed = listed

    async def list(self, github_id):
        return self.listed


def call(names=None, everything=False, irreversible=True, listed=(), to=ADDR):
    adapter, limiter = FakeAdapter(), FakeLimiter()
    result = asyncio.run(transfer.transfer(
        ME, to, names, everything, irreversible, adapter, limiter, FakeRecords(list(listed))
    ))
    return result, adapter, limiter


class Policy(unittest.TestCase):
    def refused(self, code, **kw):
        with self.assertRaises(AgentError) as cm:
            call(**kw)
        self.assertEqual(cm.exception.detail["error"], code)

    def test_without_confirmation_nothing_happens(self):
        self.refused("confirmation_required", names=["ai:gh:7"], irreversible=False)

    def test_names_or_everything_exactly_one(self):
        self.refused("invalid_selection")
        self.refused("invalid_selection", names=["ai:gh:7"], everything=True)

    def test_only_own_records(self):
        for foreign in ("ai:gh:8", "ai:gh:70", "ai:gh:7x", "ai:gh:8:mem:" + "a" * 64, "dns:example"):
            self.refused("not_your_record", names=["ai:gh:7", foreign])

    def test_too_many(self):
        self.refused("too_many_names", names=[f"ai:gh:7:mem:{i:064d}" for i in range(101)])

    def test_a_bad_address_spends_no_quota(self):
        adapter, limiter = FakeAdapter(), FakeLimiter()
        with self.assertRaises(AgentError) as cm:
            asyncio.run(transfer.transfer(
                ME, "Etypo", ["ai:gh:7"], False, True, adapter, limiter, FakeRecords([])
            ))
        self.assertEqual(cm.exception.detail["error"], "invalid_address")
        self.assertEqual((limiter.admitted, adapter.calls), ([], []))

    def test_unconfirmed_missing_or_lapsed_names_spend_no_quota(self):
        cases = {
            "record_pending": {"status": "pending"},
            "not_active": {"status": "confirmed", "expired": True},
        }
        for code, record in [*cases.items(), ("not_found", None)]:
            adapter = FakeAdapter({} if record is None else {"ai:gh:7": record})
            limiter = FakeLimiter()
            with self.assertRaises(AgentError) as cm:
                asyncio.run(transfer.transfer(
                    ME, ADDR, ["ai:gh:7"], False, True, adapter, limiter, FakeRecords([])
                ))
            self.assertEqual(cm.exception.detail["error"], code)
            self.assertEqual((limiter.admitted, adapter.calls), ([], []))

    def test_listed_names_go_out_with_a_century_and_quota_per_name(self):
        mem = "ai:gh:7:mem:" + "b" * 64
        result, adapter, limiter = call(names=["ai:gh:7", mem, "ai:gh:7"])
        self.assertEqual(adapter.calls, [(["ai:gh:7", mem], ADDR, 36500)])
        self.assertEqual(limiter.admitted, [2])
        self.assertEqual(result["to_address"], ADDR)
        self.assertIn("final", result["after"])

    def test_everything_skips_expired_records(self):
        listed = [
            {"name": "ai:gh:7", "expired": False},
            {"name": "ai:gh:7:mem:" + "c" * 64, "expired": True},
            {"name": "ai:gh:7:mem:" + "d" * 64, "expired": False},
        ]
        _, adapter, _ = call(everything=True, listed=listed)
        self.assertEqual(adapter.calls[0][0], ["ai:gh:7", "ai:gh:7:mem:" + "d" * 64])

    def test_everything_with_nothing_live(self):
        self.refused("nothing_to_transfer", everything=True, listed=[{"name": "ai:gh:7", "expired": True}])


class Registered(unittest.TestCase):
    def test_tool_is_listed_as_destructive_with_required_confirmation(self):
        from app.mcp_app import mcp

        tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
        tool = tools["transfer_records"]
        self.assertTrue(tool.annotations.destructiveHint)
        self.assertEqual(set(tool.inputSchema["required"]), {"to_address", "irreversible"})
        self.assertIn("after", tool.outputSchema["properties"])
        self.assertFalse(tool.description.startswith(" "))


if __name__ == "__main__":
    unittest.main()
