"""Network-fee funds for agents that take their records over.

Once a name leaves the gateway's wallet, changing or renewing it is a
transaction its new holder pays for, in EMC, from its own node. An agent that
has never held EMC would be left with records it cannot touch. So a transfer
may ask for a small amount of EMC to go to the same address, to pay those fees.

It is not a reward and not a faucet. The rules keep it that way:

  - only with a transfer, and only to the address that received the names;
  - once per GitHub account, ever;
  - at most `fee_grants_per_day` across everyone in the trailing 24 hours —
    zero (the default) switches grants off; production sets the ceiling in
    deploy/.env;
  - the account-age rule of every write applies, since a transfer is one.

The once-per-account mark lives in Redis like the rest of the edge's state.
Losing Redis would let an account claim a second grant; at a few hundredths of
an EMC under a daily ceiling, that is an accepted cost, not a reason for a
registry.
"""
from __future__ import annotations

import json
import secrets
import time

import redis.asyncio as redis

from .config import settings
from .errors import AgentError

DAY = 86400

# KEYS: the account's mark, the global window.  ARGV: now, token, limit.
# Reserves a grant only if the account never had one and the window has room.
# Returns 0 when reserved, 1 when the account already had one, 2 when full.
_RESERVE = """
local now = tonumber(ARGV[1])
local limit = tonumber(ARGV[3])
if redis.call('EXISTS', KEYS[1]) == 1 then
  return 1
end
redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', now - 86400)
if redis.call('ZCARD', KEYS[2]) >= limit then
  return 2
end
redis.call('SET', KEYS[1], 'reserved')
redis.call('ZADD', KEYS[2], now, ARGV[2])
redis.call('PEXPIRE', KEYS[2], 86400000)
return 0
"""

GLOBAL_KEY = "grant:day:all"


def _account_key(github_id: int) -> str:
    return f"grant:gh:{github_id}"


class FeeGrants:
    def __init__(self, url: str) -> None:
        self._redis = redis.from_url(url, decode_responses=True)
        self._script = self._redis.register_script(_RESERVE)

    async def reserve(self, github_id: int) -> str:
        """Hold this account's one grant before the transfer goes out, or raise.

        Refusing here, before any quota or chain write, lets the agent decide
        whether to transfer without funds rather than finding out afterwards."""
        if settings.fee_grants_per_day <= 0:
            raise AgentError(
                503, "fee_grant_unavailable",
                "Network-fee funds are not offered at the moment.",
                "Transfer without fee_grant, and fund the address yourself before changing the records.",
            )
        token = secrets.token_hex(8)
        refused = int(await self._script(
            keys=[_account_key(github_id), GLOBAL_KEY],
            args=[time.time(), token, settings.fee_grants_per_day],
        ))
        if refused == 1:
            raise AgentError(
                409, "fee_grant_used",
                "This account has already received its network-fee funds; they are given once.",
                "Transfer without fee_grant. The funds sent before can pay fees for any of your records.",
            )
        if refused == 2:
            raise AgentError(
                503, "fee_grant_unavailable",
                "Today's budget for network-fee funds is used up. Nothing is wrong on your side.",
                "Retry later with fee_grant, or transfer without it now.",
                retry_after=3600,
            )
        return token

    async def release(self, github_id: int, token: str) -> None:
        """Give a reservation back: the transfer or the payment did not happen."""
        await self._redis.delete(_account_key(github_id))
        await self._redis.zrem(GLOBAL_KEY, token)

    async def settle(self, github_id: int, address: str, txid: str) -> None:
        """Mark the account's grant as paid. Kept for good: it is given once."""
        await self._redis.set(_account_key(github_id), json.dumps(
            {"address": address, "txid": txid, "amount": str(settings.fee_grant_emc), "at": int(time.time())}
        ))

    async def aclose(self) -> None:
        await self._redis.aclose()
