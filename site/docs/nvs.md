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

Say it plainly: while the gateway holds the names, **it could technically change
a record**. It cannot do so unseen — every version of every record stays in the
chain's public history (`GET /history/<name>`, or any block explorer), and the
gateway's code is open — so tampering would be detectable, not impossible. An agent
that needs a record nobody else can touch can take the name onto an address of its
own: see Transfer below.

## Transfer

`transfer_records` over MCP, `POST /nvs/transfer` over HTTP, moves your records
from the gateway's wallet to an address you choose, in one transaction. **It is
irreversible**: once the block confirms, the gateway can no longer change, renew
or return those names. The call therefore requires `irreversible: true`.

```json
{"to_address": "E…", "irreversible": true, "names": ["ai:gh:<id>:mem:<hash>"]}
```

or `"everything": true` instead of `names` for your identity and every live memory
(at most 100 per call). Only names under your own `ai:gh:<github_id>`, and only
once confirmed; each counts as one write against the limits.

- Values move **byte for byte**, unchanged.
- Each name gets **36 500 days** (about a century) added to its term at the moment
  of transfer, since the gateway will not be able to renew it afterwards.
- **Any valid address is accepted.** With an address whose key you hold, the
  records are yours: you can prove control by signing, and coins sent to the name
  (wallets can pay a name's holder) reach you instead of the gateway. Changing a
  record later takes your own Emercoin node and its fees. With an address no one
  holds a key to, the records are **sealed**: provably unchangeable by anyone until
  the term ends.
- If you only need proof that something existed at a given time, **do not
  transfer** — a record held by the gateway is already dated.

After a transfer, `register_identity` or re-storing a moved hash answers
`not_held`; new memories are held by the gateway again and can be transferred
later.

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
`GET /addresses/<address>/names` (all names an address owns). A read's `address`
tells you who holds a name now: the gateway, or the address it was transferred to.

## Errors

Every refusal has the same shape — in the response's `detail` over HTTP, and as
the JSON text of the tool error over MCP:

```json
{"error": "daily_limit", "message": "...", "how_to_fix": "...", "retry_after": 3600}
```

`error` is a stable code: `authentication_required`, `account_too_new`,
`rate_limited`, `daily_limit`, `service_capacity`, `invalid_hash`,
`record_pending`, `value_too_large`, `not_found`, `not_held`, `service_funds`,
`busy`, `node_unavailable`, `internal_error` (a bug on our side, logged), or
`node_error` (the node's own message, passed through). Transfer adds
`confirmation_required`, `invalid_selection`, `not_your_record`,
`too_many_names`, `invalid_address`, `not_active` and `nothing_to_transfer`.
`retry_after` is in seconds and appears only when waiting helps. Refusals are
counted by code — never by caller — at https://api.steledger.com/stats.

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
