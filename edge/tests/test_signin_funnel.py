"""The MCP OAuth sign-in is counted step by step, so a drop-off shows."""
from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from app.oauth_provider import GitHubOAuthProvider, StaleState


class Stats:
    def __init__(self):
        self.steps = []

    async def record_signin(self, step):
        self.steps.append(step)


class Redis:
    def __init__(self):
        self.d = {}

    async def set(self, k, v, ex=None):
        self.d[k] = v

    async def get(self, k):
        return self.d.get(k)

    async def getdel(self, k):
        return self.d.pop(k, None)

    async def exists(self, k):
        return k in self.d


class GitHub:
    def __init__(self, ok=True):
        self.ok = ok

    def authorize_url(self, state):
        return f"https://github.com/login/oauth/authorize?state={state}"

    async def exchange_code(self, code):
        return {"access_token": "gho_x"} if self.ok else {"error": "bad_verification_code"}

    async def fetch_user(self, token):
        return 7, "someone", 1_600_000_000


def provider(ok=True):
    p = GitHubOAuthProvider()
    p._github, p._redis, p._stats = GitHub(ok), Redis(), Stats()
    return p


CLIENT = OAuthClientInformationFull(client_id="c1", redirect_uris=[AnyUrl("https://client.example/cb")])
PARAMS = AuthorizationParams(
    state="s1", scopes=["agent"], code_challenge="x" * 43,
    redirect_uri=AnyUrl("https://client.example/cb"), redirect_uri_provided_explicitly=True,
)


async def test_a_full_sign_in_counts_every_step():
    p = provider()
    await p.register_client(CLIENT)
    await p.authorize(CLIENT, PARAMS)
    redirect = await p.complete_github("ghcode", "s1")
    code = redirect.split("code=")[1].split("&")[0]
    ac = await p.load_authorization_code(CLIENT, code)
    await p.exchange_authorization_code(CLIENT, ac)
    assert p._stats.steps == ["client_registered", "authorize_started", "github_ok", "token_issued"]


async def test_failures_are_counted_where_they_happen():
    p = provider(ok=False)
    await p.authorize(CLIENT, PARAMS)
    await p.complete_github("ghcode", "s1")
    with pytest.raises(StaleState):
        await p.complete_github("ghcode", "s1")  # the state was spent: now it is stale
    assert p._stats.steps == ["authorize_started", "github_failed", "state_expired"]


class GitHubDown(GitHub):
    async def exchange_code(self, code):
        raise httpx.ConnectError("github unreachable")


@pytest.mark.parametrize("github, error", [
    (GitHub(ok=False), "access_denied"),         # GitHub refused the code (bad_verification_code)
    (GitHubDown(), "temporarily_unavailable"),   # GitHub could not be asked at all
])
async def test_a_failed_github_step_goes_back_to_the_client_as_an_oauth_error(github, error):
    # Not a 500: the MCP client gets its redirect with error + state and can tell the user.
    p = provider()
    p._github = github
    await p.authorize(CLIENT, PARAMS)
    url = urlsplit(await p.complete_github("ghcode", "s1"))
    q = parse_qs(url.query)
    assert f"{url.scheme}://{url.netloc}{url.path}" == "https://client.example/cb"
    assert q["error"] == [error] and q["state"] == ["s1"] and "code" not in q
    assert q["error_description"][0]


def test_the_callback_route_never_answers_500(monkeypatch):
    from fastapi.testclient import TestClient

    from app import main

    async def owns(state):
        return True

    main.app.dependency_overrides[main.get_github] = lambda: None
    main.app.dependency_overrides[main.get_oauth] = lambda: None
    monkeypatch.setattr(main.mcp_oauth, "owns_state", owns)
    try:
        c = TestClient(main.app, follow_redirects=False)

        async def refused(code, state):
            return "https://client.example/cb?error=access_denied&state=s1"
        monkeypatch.setattr(main.mcp_oauth, "complete_github", refused)
        r = c.get("/auth/github/callback", params={"code": "foo", "state": "s1"})
        assert (r.status_code, r.headers["location"]) == (302, "https://client.example/cb?error=access_denied&state=s1")

        async def raced(code, state):
            raise StaleState("invalid or expired state")
        monkeypatch.setattr(main.mcp_oauth, "complete_github", raced)
        r = c.get("/auth/github/callback", params={"code": "foo", "state": "s1"})
        assert r.status_code == 400 and "already used or has expired" in r.text
    finally:
        main.app.dependency_overrides.clear()
