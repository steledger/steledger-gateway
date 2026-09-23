---
name: steledger-identity
description: Give an AI agent a verifiable, durable on-chain identity and memory via the steledger MCP tools — anchored on a public blockchain, so a record stays checkable independently of any vendor. Use when an agent needs to register its identity, store hashes of research/memories/artifacts on-chain, prove who it is by signature, or read another agent's on-chain identity or records.
---

# On-chain identity & memory for agents (Steledger)

This project (steledger-gateway) gives AI agents a durable identity and memory
through the **`steledger` MCP server**, anchored in the Name-Value Storage of the
Emercoin blockchain — named so a record can be verified in a public explorer. Agents
need **no cryptocurrency** — the gateway's hot-wallet pays for every record.

Read `AGENTS.md` for the full rationale and trust model. This skill is the
operational checklist for *using* the tools.

## When to use

- An agent wants a durable, verifiable identity → register an identity record.
- An agent wants to remember something (research result, document, prior memory)
  in a tamper-evident way → store its **hash** on-chain (keep the body off-chain).
- You need to look up an agent's identity or a stored record → read it back.

## Prerequisites

The MCP server must be configured (see `AGENTS.md` → Quickstart). It needs the
gateway reachable at `GATEWAY_URL`. Auth is a session JWT obtained with the
device-flow `login()` + `login_poll()`, or a pre-set `GATEWAY_JWT` env var.

## Tools (steledger MCP)

| Tool | Use it to |
|------|-----------|
| `node_status()` | check the node is synced before relying on reads |
| `login()` + `login_poll(session_id)` | get a session JWT via GitHub device flow (GitHub identity = root of trust) |
| `register_identity(address, metadata?)` | create/refresh this agent's `ai:gh:<id>` record |
| `store_memory(content_hash, metadata?)` | record one memory/research hash |
| `store_memory_batch(records)` | record many hashes in ONE atomic transaction |
| `read_record(name)` | read any record by NVS name |
| `list_records(github_id?)` | list every record under a GitHub id (yours if omitted) |

## Standard flows

### 1. Establish identity (once)
1. `login()` → show the returned `user_code` + `verification_uri` to the user;
   after they authorize on GitHub, `login_poll(<session_id>)` caches the JWT.
2. Obtain an Emercoin address as the agent's signing key (the operator funds /
   manages the gateway wallet; for a self-sovereign key, generate one and supply
   its address). You can register first and add/rotate the address later.
3. `register_identity(<address>, {"role": "...", "model": "..."})`.
   This writes `ai:gh:<github_id>` binding the GitHub id ↔ address on-chain.
   Re-registering with a new address rotates the key (same record, value update).

### 2. Store a memory / research hash
- Hash the artifact yourself; store the **body off-chain** (IPFS, object store).
- One item: `store_memory("<sha256>", {"kind": "research", "ref": "<uri>"})`.
- Many items: `store_memory_batch([{ "content_hash": "...", "metadata": {...} }, ...])`
  — atomic and cheaper (one transaction). Prefer this for >1 record.

### 3. Read back
- `read_record("ai:gh:<id>")` for an identity, or
  `read_record("ai:gh:<id>:mem:<hash>")` for a memory record.
- A just-written record returns `status: "pending"` (in the mempool) and flips to
  `status: "confirmed"` after the next block (typically within ~10 min, but a
  block can land in seconds or take 40+ min). Don't treat `pending` as failure;
  re-read later to confirm.

## Conventions & limits

- **Never put secrets or raw bodies on-chain** — only hashes + small metadata.
  NVS records are public and permanent for their lifetime.
- **Write limits**: free tier allows 10 NVS writes per minute and 100 per trailing
  24 hours (a batch of N counts as N); the GitHub account must be ≥ 30 days old.
- **Names are namespaced** per GitHub id: `ai:gh:<id>` (identity),
  `ai:gh:<id>:mem:<hash>` (memory). Don't write outside your namespace.
- **Records expire** (default 1825 days, about five years of wall clock — check `expires_in`)
  unless refreshed. Re-writing adds to the remaining term. A lapsed name is
  released and anyone may register it, so expiry is an impersonation risk, not
  just data going stale — check `expired` / `expires_in` before trusting a read.

## If something fails

- 401 / "not authenticated" → run `login()` + `login_poll()` first (or set `GATEWAY_JWT`).
- 429 → a per-account limit (per minute or per 24 hours); the message says which.
  Batch your writes, or wait.
- 403 "account too new" → the GitHub account is under 30 days old; the message
  gives the date writes open. Reads work meanwhile.
- 503 on a write → the service's daily capacity is reached; retry later.
- `read_record` 404 right after a write → it's still pending; re-read after a block.
- Every error is JSON: read `error` (a stable code) and `how_to_fix`; honour `retry_after`.
- New session, hashes forgotten → `list_records()` returns them with their metadata.
- Check `node_status().synced` — reads of confirmed names need a synced node.
