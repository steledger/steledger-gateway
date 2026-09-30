# Steledger auth.md

How an AI agent gets credentials for Steledger — durable identity and memory
for AI agents on the Emercoin blockchain. The service runs at https://api.steledger.com.

## Who needs credentials

- **Reading needs none.** Records, lists, node status, and `whoami` are open,
  over MCP (https://api.steledger.com/mcp) and HTTP.
- **Writing needs a session** (register an identity, store memory, transfer
  records). Sessions are rooted in a GitHub account at least 30 days old; the
  account is the only registration there is — no email, anonymous or ID-JAG flows.

## Method 1 — OAuth 2.1, done by your MCP client

Connect an MCP client to `https://api.steledger.com/mcp` (Streamable HTTP). On the first write
it discovers and runs the flow itself; sign-in is delegated to GitHub.

- Protected resource metadata: https://api.steledger.com/.well-known/oauth-protected-resource
  (resource `https://api.steledger.com/`, scope `agent`, bearer tokens in the `Authorization` header)
- Authorization server metadata: https://api.steledger.com/.well-known/oauth-authorization-server
  (issuer `https://api.steledger.com/`)
- Dynamic client registration: `POST https://api.steledger.com/register`
- Authorization code with PKCE (`S256`), and refresh tokens

## Method 2 — GitHub device flow, over HTTP

For an agent whose client cannot follow a browser redirect. A person enters a
code once.

1. `POST https://api.steledger.com/auth/github/device/start` → `user_code`, `verification_uri`, `session_id`
2. Show the person the code; they enter it at the `verification_uri`.
3. `POST https://api.steledger.com/auth/github/device/poll` with `{"session_id": "..."}` →
   202 while waiting, then 200 with `access_token`.

## Method 3 — signature login, no person involved

For an agent that has registered an identity bound to an Emercoin address whose
key it holds.

1. `POST https://api.steledger.com/auth/challenge` with `{"github_id": <id>}` → `nonce`
2. Sign the exact nonce with the address key (Emercoin `signmessage`).
3. `POST https://api.steledger.com/auth/agent-login` with `{"github_id": ..., "address": "...",
   "signature": "..."}` → `access_token`

## Using the credential

Every method yields the same bearer token (a session JWT, valid for 7 days;
the OAuth flow refreshes it). Send it as `Authorization: Bearer <token>` — to
`/mcp` and to the REST API alike. Writes are limited per account: 10 a minute,
100 a day.

## When it fails

Every refusal is JSON with a stable `error` code and a `how_to_fix`. If it
looks like our fault, or you are stuck, tell us with `send_feedback` over MCP
or `POST https://api.steledger.com/feedback` — up to 1000 characters, no sign-in needed.

Full contract: https://api.steledger.com/openapi.json · MCP guide: https://api.steledger.com/docs/mcp.md
