"""Authn/authz for the edge.

GitHub identity is the trust anchor. We do NOT keep an agent registry — a valid
GitHub identity is exchanged for a self-contained session JWT carrying the
github_id + tariff. Every protected route just verifies the JWT signature.

Dev login (this module): caller presents a GitHub token, we resolve it to a
GitHub user via the API. The agent-signature login path is in `main`.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import httpx
import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import settings
from .github import parse_created

GITHUB_USER_API = "https://api.github.com/user"


@dataclass
class Principal:
    github_id: int
    github_login: str
    tariff: str
    # When the GitHub account was created (Unix seconds), from the `ghc` claim.
    # None for tokens that did not come through GitHub — signature login, or
    # tokens issued before the claim existed — and those skip the age check:
    # signature login needs an on-chain identity, which needed a write, which
    # needed the account to be old enough already.
    github_created: int | None = None


async def resolve_github_token(token: str) -> tuple[int, str, int | None]:
    """Verify a GitHub token and return (id, login, created). Raises 401 if invalid."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            GITHUB_USER_API,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        )
    if resp.status_code != 200:
        raise HTTPException(status_code=401, detail="invalid GitHub token")
    data = resp.json()
    return int(data["id"]), data["login"], parse_created(data.get("created_at"))


def issue_jwt(
    github_id: int, github_login: str, tariff: str = "free", github_created: int | None = None
) -> str:
    now = int(time.time())
    payload = {
        "sub": str(github_id),
        "login": github_login,
        "tariff": tariff,
        "iat": now,
        "exp": now + settings.jwt_ttl_seconds,
    }
    if github_created is not None:
        payload["ghc"] = github_created
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def decode_token(token: str) -> Principal | None:
    """Verify a session JWT and return the Principal, or None if invalid.

    A non-raising variant of `current_principal`, used outside the FastAPI
    dependency system (e.g. the MCP /mcp auth middleware)."""
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        return None
    return _principal_from(payload)


def _principal_from(payload: dict) -> Principal:
    return Principal(
        github_id=int(payload["sub"]),
        github_login=payload["login"],
        tariff=payload.get("tariff", "free"),
        github_created=payload.get("ghc"),
    )


_bearer = HTTPBearer(auto_error=True)


def current_principal(creds: HTTPAuthorizationCredentials = Depends(_bearer)) -> Principal:
    try:
        payload = jwt.decode(creds.credentials, settings.jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail=f"invalid token: {exc}")
    return _principal_from(payload)
