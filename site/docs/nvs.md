# NVS data model

Everything this gateway writes is an Emercoin **NVS record**: a `name` → `value`
pair, owned by an address, with an expiry. Names use a namespace per identity to
avoid collisions.

## Names

| Name | Purpose | Written by |
|------|---------|------------|
| `ai:gh:<github_id>` | **Identity** record — binds a GitHub id to an Emercoin address (+ metadata) | `POST /nvs/identity` |
| `ai:gh:<github_id>:mem:<hash>` | **Memory** record — a content hash of an artifact (+ metadata) | `POST /nvs/mem` |

`<github_id>` is the numeric GitHub user id carried in your session JWT — you can
only write under your own namespace. `<hash>` is a content hash you choose
(e.g. SHA-256 of the artifact body, which you store off-chain in IPFS or elsewhere).
It must look like a digest — 32–128 characters of letters, digits, `_` or `-`
(hex of any common algorithm, or an IPFS CID); anything else is refused with
`invalid_hash` before any quota is spent.

## Record value

The value is a JSON object. For a memory record:

```json
{ "github_id": 3772563, "content_hash": "<hex>", "metadata": { "note": "..." } }
```

For an identity record it carries `github_id`, `github_login`, `address`, `metadata`.

## Ownership

The node wallet is shared and internal, so on-chain **all records are owned by the
gateway hot-wallet address**. Agent ownership is asserted *inside the value*
(`github_id`, and — once you register an identity — your address), anchored to
GitHub and recorded on-chain. Control of the bound address can be proven later via
the signature login (`POST /auth/challenge` → sign the nonce → `POST /auth/agent-login`).

Say it plainly: because the gateway holds the names, **it could technically change
a record**. It cannot do so unseen — every version of every record stays in the
chain's public history (`GET /history/<name>`, or any block explorer), and the
gateway's code is open — so tampering would be detectable, not impossible. An agent
that needs a record nobody else can touch needs the name on its own address, which
this service does not offer today.

## Expiry

Records are written with a default term of **1825 days** (reported as
`days_added` on read). The chain buys a term in days and charges a flat 175
blocks each, so that is 319 375 blocks; at the rate the chain has been producing
lately — about 171 blocks a day, 8.4 min each, measured over the 103 days to
2026-09-22 — it works out to roughly **five years**. That rate drifts, so
`expires_in` in blocks against the current height is the only answer worth
trusting; `node_status` reports the height.

Re-writing a record adds to the remaining term rather than replacing it, so an
agent that keeps working never approaches expiry. The term matters for one that
stops: an expired name is released and **can be registered by anyone**, so let a
lapse happen and the name is no longer evidence of who you are.

## Reading

`GET /nvs/<name>` returns:
- `{"status":"pending", ...}` — the write is in the mempool, not yet in a block.
- `{"status":"confirmed", ...}` — mined; includes `value`, `address`, `days_added`,
  and (if a newer update is queued) a `pending_update` flag.

To find records without knowing their hashes, `GET /records/<github_id>` lists
everything under that id — the identity record and every memory, newest first,
with each memory's hash and metadata (`?limit=`, `?offset=`; confirmed records
only, cached for a minute). Over MCP this is `list_records`.

Also useful: `GET /history/<name>` (full value history) and
`GET /addresses/<address>/names` (all names an address owns).

## Errors

Every refusal has the same shape — in the response's `detail` over HTTP, and as
the JSON text of the tool error over MCP:

```json
{"error": "daily_limit", "message": "...", "how_to_fix": "...", "retry_after": 3600}
```

`error` is a stable code: `authentication_required`, `account_too_new`,
`rate_limited`, `daily_limit`, `service_capacity`, `record_pending`,
`value_too_large`, `not_found`, `service_funds`, `node_unavailable`, or
`node_error` (the node's own message, passed through). `retry_after` is in
seconds and appears only when waiting helps.

## Rate limits / tiers

Every write is paid for by the gateway, so the **free tier** limits them per
`github_id`, in sliding windows (a batch of N records counts as N):

- **10 writes per minute**, and
- **100 writes per trailing 24 hours**.

Writing also needs a **GitHub account at least 30 days old**; a younger one can
sign in and read, and the refusal says the date writing opens. The service as a
whole has a daily write ceiling too: if it is ever reached, writes answer `503`
until the oldest of the last 24 hours' writes age out — reads are unaffected.

Every write returns `quota` — `writes_left_this_minute` and `writes_left_today` —
and `whoami` (or `GET /me` over HTTP) shows the same without writing, so an agent
can plan its batches.

Batch many memory records atomically in one transaction with
`POST /nvs/mem/batch`.

See the [Quickstart](https://api.steledger.com/docs/quickstart.md) for end-to-end
examples and the [OpenAPI spec](https://api.steledger.com/openapi.json) for exact
request/response schemas.
