"""Adapter settings. Network (regtest/testnet/mainnet) is just the RPC target."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ADAPTER_", env_file=".env", extra="ignore")

    # Node RPC — points at the emc service over the internal docker network.
    rpc_url: str = "http://emc:6662"
    rpc_user: str = "emcrpc"
    rpc_password: str = "emcpass"

    # NVS record lifetime in days (records expire on Emercoin).
    # 1825 days as the chain counts them — it converts at a flat 175 blocks/day
    # while actually producing ~122, so this is roughly seven years of wall clock.
    # Long on purpose: an expired name can be re-registered by anyone, so a lapse
    # is an impersonation window, not merely a record going stale. The fee barely
    # moves with the term (it is square-rooted), and days accumulate on re-write,
    # so this governs only the agents that fall silent.
    nvs_default_days: int = 1825

    # Shared-secret gate. Empty = open (dev, browse /docs freely). When set, every
    # request must carry `X-Internal-Key: <internal_key>`. Use it when the adapter
    # is reachable beyond a trusted docker network (e.g. wallet at home, edge on a
    # VPS) — there a network barrier alone isn't enough.
    internal_key: str = ""


settings = Settings()
