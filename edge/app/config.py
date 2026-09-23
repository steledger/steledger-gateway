"""Edge settings — the agent-facing IAM layer in front of the node adapter."""
from __future__ import annotations

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EDGE_", env_file=".env", extra="ignore")

    # The node adapter (RPC↔REST). Edge never speaks RPC itself.
    adapter_url: str = "http://adapter:8000"
    # Must match the adapter's ADAPTER_INTERNAL_KEY when that gate is enabled.
    adapter_key: str = ""

    # Public base URL — the OAuth issuer advertised in the MCP /.well-known metadata.
    public_url: str = "https://api.steledger.com"

    # Auth: self-contained session JWT (no agent registry; we trust GitHub ID).
    # 7 days — long enough for a pasted token; OAuth clients also auto-refresh.
    jwt_secret: str = "dev-insecure-change-me"
    jwt_ttl_seconds: int = 604800

    # GitHub OAuth App — identity bootstrap. Device flow needs only the client_id;
    # the web flow also needs the secret + redirect_uri. Scope is empty (we read
    # only the public id+login via GET /user).
    github_client_id: str = ""
    github_client_secret: str = ""
    github_redirect_uri: str = "http://localhost:8000/auth/github/callback"

    # Login surfaces. Device flow is always on. The raw-token /auth/login is a
    # dev/CI shortcut; the browser web flow is opt-in until a public domain exists.
    dev_login_enabled: bool = False
    web_login_enabled: bool = False

    # Free tier write limits, all sliding windows in Redis (see ratelimit.py).
    # Per account: a burst limit per minute and a cap per trailing 24 hours.
    free_tier_writes_per_min: int = 10
    free_tier_writes_per_day: int = 100
    # Across all accounts per trailing 24 hours: the ceiling on what the gateway
    # wallet can be made to spend in a day. Production sets its own value in
    # deploy/.env; this default only has to be safe, not right.
    global_writes_per_day: int = 1000
    # A GitHub account must be this old before it can write. Fresh accounts are
    # free to mint in bulk, and every write spends the gateway's EMC.
    min_account_age_days: int = 30
    redis_url: str = "redis://redis:6379/0"

    # NVS record lifetime in days (records expire on Emercoin).
    # 1825 days as the chain counts them. It converts at a flat 175 blocks/day and
    # currently produces about 171 (8.4 min/block, measured over the 103 days to
    # 2026-09-22), so this is roughly five years of wall clock — but block rate
    # drifts, and only `expires_in` against the current height is authoritative.
    # Long on purpose: an expired name can be re-registered by anyone, so a lapse
    # is an impersonation window, not merely a record going stale. The fee barely
    # moves with the term (it is square-rooted), and days accumulate on re-write,
    # so this governs only the agents that fall silent.
    nvs_default_days: int = 1825

    @model_validator(mode="after")
    def _require_strong_secret(self) -> "Settings":
        # In a real deployment (dev_login disabled) refuse to start on a weak HS256
        # secret — this is what stops the dev secret leaking into prod. Dev keeps
        # the short secret for convenience (it sets EDGE_DEV_LOGIN_ENABLED=true).
        if not self.dev_login_enabled and len(self.jwt_secret.encode()) < 32:
            raise ValueError(
                "EDGE_JWT_SECRET must be >=32 bytes when EDGE_DEV_LOGIN_ENABLED is false"
            )
        return self


settings = Settings()
