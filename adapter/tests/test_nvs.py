"""What the adapter sends to the node, argument for argument.

The node reads a `valuetype` other than "", "hex" or "base64" as a file path on
its own host and writes that file on-chain, and `toaddress` sits right before it
positionally. So these tests pin the exact RPC calls rather than the outcome: a
stray or shifted argument is the failure they exist to catch.

    cd adapter && uv run --with fastapi --with httpx --with pydantic-settings \
        python -m unittest discover -s tests
"""
from __future__ import annotations

import asyncio
import unittest

from fastapi.testclient import TestClient

from app import main, nvs
from app.rpc import RPCError

ADDR = "EdCh5nZ9QuTrPbkmZXGm4e5r3iPpHS8xKM"


class FakeRPC:
    """Records every call; answers from a table of name_show results."""

    def __init__(self, shown=None, mempool=(), invalid=(), fail=None):
        self.calls: list[tuple] = []
        self.shown = shown or {}
        self.mempool = list(mempool)
        self.invalid = set(invalid)
        self.fail = fail

    async def call(self, method, *params):
        self.calls.append((method, *params))
        if method == "getaddressinfo":
            return {"ismine": params[0] == "em1qours"}
        if method == "name_show":
            name = params[0]
            if name not in self.shown:
                raise RPCError(-4, "failed to read from name DB")
            return self.shown[name]
        if method == "name_mempool":
            return [{"name": n} for n in self.mempool]
        if method == "validateaddress":
            return {"isvalid": params[0] not in self.invalid}
        if self.fail:
            raise RPCError(-4, self.fail)
        return "txid"

    def sent(self, method):
        return [c for c in self.calls if c[0] == method]


def run(coro):
    return asyncio.run(coro)


class WriteRecord(unittest.TestCase):
    def test_new_name_spells_out_toaddress_and_valuetype(self):
        rpc = FakeRPC()
        run(nvs.write_record(rpc, "ai:gh:1", {"a": 1}, 1825))
        self.assertEqual(rpc.sent("name_new"), [("name_new", "ai:gh:1", '{"a":1}', 1825, "", "")])

    def test_held_name_is_updated(self):
        rpc = FakeRPC(shown={"ai:gh:1": {"value": "x"}})
        run(nvs.write_record(rpc, "ai:gh:1", "v", 30))
        self.assertEqual(rpc.sent("name_update"), [("name_update", "ai:gh:1", "v", 30, "", "")])

    def test_expired_name_is_taken_back_with_new(self):
        rpc = FakeRPC(shown={"ai:gh:1": {"value": "x", "expired": True}})
        run(nvs.write_record(rpc, "ai:gh:1", "v", 30))
        self.assertEqual(len(rpc.sent("name_new")), 1)

    def test_a_path_shaped_value_is_still_just_text(self):
        rpc = FakeRPC()
        run(nvs.write_record(rpc, "n", "/root/.emercoin/emercoin.conf", 30))
        (call,) = rpc.sent("name_new")
        self.assertEqual(call[-1], "")


class WriteBatch(unittest.TestCase):
    def test_new_update_and_pending_each_get_the_right_verb(self):
        rpc = FakeRPC(
            shown={"held": {"value": "x"}, "lapsed": {"value": "x", "expired": True}},
            mempool=["pending"],
        )
        ops = [{"name": n, "value": {"k": n}, "days": 7} for n in ("fresh", "held", "lapsed", "pending")]
        run(nvs.write_batch(rpc, ops))
        (call,) = rpc.sent("name_updatemany")
        self.assertEqual(call[1], [
            {"NEW": "fresh", "value": '{"k":"fresh"}', "days": 7, "toaddress": "", "valuetype": ""},
            {"UPDATE": "held", "value": '{"k":"held"}', "days": 7, "toaddress": "", "valuetype": ""},
            {"NEW": "lapsed", "value": '{"k":"lapsed"}', "days": 7, "toaddress": "", "valuetype": ""},
            {"UPDATE": "pending", "value": '{"k":"pending"}', "days": 7, "toaddress": "", "valuetype": ""},
        ])
        self.assertEqual(len(rpc.sent("name_mempool")), 1, "the mempool is read once per batch")


class Transfer(unittest.TestCase):
    def test_values_are_carried_over_as_base64(self):
        rpc = FakeRPC(shown={"a": {"value": "eyJ4IjoxfQ=="}, "b": {"value": "Yg=="}})
        run(nvs.transfer(rpc, ["a", "b"], ADDR, 36500))
        self.assertEqual(rpc.sent("name_show"), [("name_show", "a", "base64"), ("name_show", "b", "base64")])
        (call,) = rpc.sent("name_updatemany")
        self.assertEqual(call[1], [
            {"UPDATE": "a", "value": "eyJ4IjoxfQ==", "days": 36500, "toaddress": ADDR, "valuetype": "base64"},
            {"UPDATE": "b", "value": "Yg==", "days": 36500, "toaddress": ADDR, "valuetype": "base64"},
        ])

    def test_invalid_address_is_refused_before_anything_is_read(self):
        rpc = FakeRPC(invalid=["legacy"])
        with self.assertRaises(nvs.TransferError) as cm:
            run(nvs.transfer(rpc, ["a"], "legacy", 1))
        self.assertEqual(cm.exception.status_code, 400)
        self.assertEqual([c[0] for c in rpc.calls], ["validateaddress"])

    def test_expired_or_missing_names_are_refused(self):
        rpc = FakeRPC(shown={"old": {"value": "", "expired": True}})
        for name, status in (("old", 409), ("gone", 404)):
            with self.assertRaises(nvs.TransferError) as cm:
                run(nvs.transfer(rpc, [name], ADDR, 1))
            self.assertEqual(cm.exception.status_code, status)
        self.assertEqual(rpc.sent("name_updatemany"), [])


class Holder(unittest.TestCase):
    def test_free_ours_and_foreign(self):
        rpc = FakeRPC(shown={
            "ours": {"address": "em1qours"},
            "gone": {"address": ADDR},
            "lapsed": {"address": ADDR, "expired": True},
        })
        states = {n: run(nvs.holder(rpc, n))["state"] for n in ("ours", "gone", "lapsed", "never")}
        self.assertEqual(states, {"ours": "ours", "gone": "foreign", "lapsed": "free", "never": "free"})


class TransferEndpoint(unittest.TestCase):
    def client(self, rpc):
        main.app.dependency_overrides[main.get_rpc] = lambda: rpc
        self.addCleanup(main.app.dependency_overrides.clear)
        return TestClient(main.app)

    def test_duplicates_are_refused(self):
        rpc = FakeRPC()
        r = self.client(rpc).post("/nvs/transfer", json={"names": ["a", "a"], "toaddress": ADDR, "days": 1})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(rpc.calls, [])

    def test_a_name_held_elsewhere_is_a_conflict_not_a_fault(self):
        rpc = FakeRPC(shown={"a": {"value": "YQ=="}}, fail="prev name tx is not yours or is not spendable")
        r = self.client(rpc).post("/nvs/transfer", json={"names": ["a"], "toaddress": ADDR, "days": 1})
        self.assertEqual(r.status_code, 409)
        self.assertIn("is not yours", r.json()["detail"])

    def test_address_check_is_the_nodes(self):
        rpc = FakeRPC(invalid=["Etypo"])
        c = self.client(rpc)
        self.assertEqual(c.get(f"/addresses/{ADDR}/valid").json(), {"address": ADDR, "isvalid": True})
        self.assertFalse(c.get("/addresses/Etypo/valid").json()["isvalid"])
        self.assertEqual(rpc.calls, [("validateaddress", ADDR), ("validateaddress", "Etypo")])

    def test_success(self):
        rpc = FakeRPC(shown={"a": {"value": "YQ=="}})
        r = self.client(rpc).post("/nvs/transfer", json={"names": ["a"], "toaddress": ADDR, "days": 1})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"txid": "txid", "count": 1, "names": ["a"], "toaddress": ADDR})


class SlowRPC(FakeRPC):
    """name_show takes a while, like a node under load."""

    async def call(self, method, *params):
        if method == "name_show":
            await asyncio.sleep(0.3)
        return await super().call(method, *params)


class PublicReads(unittest.TestCase):
    def test_reads_beyond_the_slots_are_turned_away_and_writes_checks_are_not(self):
        import httpx

        async def scenario():
            rpc = SlowRPC(shown={"n": {"value": "v", "address": "em1qours"}})
            main.app.dependency_overrides[main.get_rpc] = lambda: rpc
            old_wait, main.READ_WAIT = main.READ_WAIT, 0.1
            try:
                transport = httpx.ASGITransport(app=main.app)
                async with httpx.AsyncClient(transport=transport, base_url="http://a") as c:
                    reads = [c.get("/nvs/n") for _ in range(main.READ_SLOTS + 2)]
                    holder = c.get("/holder/n")
                    *read_rs, holder_r = await asyncio.gather(*reads, holder)
            finally:
                main.READ_WAIT = old_wait
                main.app.dependency_overrides.clear()
            return sorted(r.status_code for r in read_rs), holder_r.status_code

        codes, holder_code = run(scenario())
        self.assertEqual(codes, [200] * main.READ_SLOTS + [503, 503])
        self.assertEqual(holder_code, 200)


if __name__ == "__main__":
    unittest.main()
