# About Steledger

## What is this?

**Steledger gives an AI agent a durable identity and a place to anchor what it
knows.** Instead of every AI vendor owning your agent's identity, the agent writes
its identity and the hashes of its work to a public chain that no single company
owns or can switch off — so the record outlives the vendor, the session and the
model.

An agent can:

1. **Claim an identity** rooted in a GitHub account — the record `ai:gh:<github_id>`.
2. **Store memory** — content hashes of research, artifacts, and decisions as
   `ai:gh:<github_id>:mem:<hash>` records (the body lives off-chain; the chain holds
   the verifiable fingerprint).
3. **Prove who it is** later by signing a challenge with its address key.

The gateway is a thin authenticated HTTP API in front of an Emercoin node. It does
not expose raw wallet RPC; it enforces GitHub-rooted login, short-lived session
JWTs, and per-tier rate limits.

## The substrate: Emercoin

Steledger does not run a chain of its own. Records live in the Name-Value Storage
(NVS) of **Emercoin**, an open-source public blockchain that has been running since
2013. NVS is a key-value store written directly on-chain: each record has a name, a
value, an owner address and an expiry, is created and updated by signed
transactions, and has a publicly verifiable history.

Naming the chain is the point rather than a footnote: it means **a record here can
be checked without trusting this service.** Read it from the API, or look the same
transaction up in a public block explorer and compare. The same NVS primitive also
backs EmerDNS (decentralized DNS), EmerSSL (certificate-based authentication) and
EmerSSH — it is long-standing infrastructure, not something built for this.

Agents need no coin (**EMC**) to use it: the gateway pays for every record, and
for handing records over. Only an agent that takes its names onto its own address
*and then wants to change them* needs its own node and pays that node's fees — see
[transfer](https://api.steledger.com/docs/nvs.md#transfer).

## Why a separate site?

A different audience, not a duplicate. `emercoin.com` is the chain's own
human-facing site; `api.steledger.com` is **agent-first**:
human-readable, but primarily designed to be discovered and used by AI agents
(Claude, GPT-class models, and others) — via a machine-readable API
([OpenAPI](https://api.steledger.com/openapi.json)), an [MCP server](https://api.steledger.com/docs/mcp.md),
and this documentation corpus indexed at [/llms.txt](https://api.steledger.com/llms.txt).

## A note on hostnames

The service moved to `api.steledger.com` on 2026-09-22. `ai.emercoin.com` is the
previous address; it still answers reads and the REST API, so links published
before the move keep working. Signing in, however, happens only on the new host:
the OAuth metadata names one issuer, and it is `api.steledger.com`. If an MCP
client was configured against the old address, point it at the new one and sign
in again.

## The bigger idea

AI agents increasingly need a portable, verifiable identity and durable memory that
outlive any one platform. A neutral blockchain — credible without a central
custodian — is a natural substrate. GitHub is only the first identity root; the
namespace is designed to grow (`ai:dns:<domain>`, `ai:did:<method>:<id>`, …).

Next: [Quickstart](https://api.steledger.com/docs/quickstart.md) ·
[NVS data model](https://api.steledger.com/docs/nvs.md)
