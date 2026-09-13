# AgentMarket OS

A B2A (business-to-agent) commerce platform: an e-commerce backend built to sell
to autonomous AI shopping agents rather than to people. Built for the UAVS
Hackathon 2026, FPT Australasia challenge *"The B2A Shift: Adapting Retail for AI
Shopping Agents"*, implementing the architecture in the accompanying proposal.

Three subsystems over a shared event backbone, an agent-facing gateway, and an
operations UI:

1. **Algorithmic Pricing Engine** (§4) — market-making-style pricing off a live
   competitor feed, with a learned spread and deterministic guardrails.
2. **Agentic RAG Storefront** (§5) — a LangGraph workflow over semantic search
   and a structured product graph, emitting machine-readable offers.
3. **Deterministic Verification Pipeline** (§6) — GS1 EPCIS provenance →
   Ed25519-signed, VC-shaped trust tokens in a hash-anchored ledger → a
   pass/fail gate that payment cannot bypass.

```
docker compose -f infra/docker-compose.yml up --build     # or: make up
```

Then open **http://localhost:3000** (Agent Demo Console) and
**http://localhost:3000/ops** (Ops Dashboard). Gateway API docs are at
http://localhost:8080/docs.

First start pulls the Ollama models (~2 GB) before the services come up.

---

## What actually runs

| Component | Technology | Notes |
|---|---|---|
| Event backbone | **Kafka** (KRaft, no ZooKeeper) | Keyed by business entity so per-SKU ordering holds; manual offset commits for at-least-once |
| Feature store | **Redis** (online) + **Postgres** (offline) | Hot path never touches disk; every observation retained for audit |
| Product graph | **PostgreSQL 16** | Public and commercial columns split at the type level |
| Vector search | **pgvector** (cosine, ivfflat) | Semantic ranking and hard predicates in one SQL statement |
| Embeddings | **Ollama** `nomic-embed-text` | Local; deterministic hashed-n-gram fallback when absent |
| Agent orchestration | **LangGraph** | The real graph from proposal Figure 3 |
| Planner / repair LLM | **Ollama** `llama3.2` | Local; only touches query understanding, never facts |
| Trust tokens | **Ed25519** + W3C VC shape | Real signing, key persisted outside the container |
| Ledger | **Postgres hash chain** | Tamper-evidence is checkable, not asserted |
| Payments | **AP2** mandates, x402 / card rails | Real mandate signing; settlement is mocked |
| Gateway | **FastAPI** + JWT | OAuth2/mTLS at an Envoy/Kong edge in production |
| UI | **Next.js 15**, Tailwind, React Flow, Recharts | Live LangGraph replay + ops dashboard |

Five services — `gateway`, `pricing`, `storefront`, `verification`, `payments` —
each a thin FastAPI app over the shared library in `libs/agentmarket_core`.

## Every backend is swappable by configuration

The infrastructure sits behind ports with more than one adapter, chosen by env
var alone. Nothing above the adapter layer knows which one is live, so the same
code runs with a Kafka cluster or with nothing installed at all.

| Port | Production | Alternatives |
|---|---|---|
| `EVENT_BUS_BACKEND` | `kafka` | `redis` (Streams — real broker, one process), `memory` (tests) |
| `FEATURE_STORE_BACKEND` | `redis` | `memory` |
| `PRODUCT_STORE_BACKEND` | `postgres` | `json` |
| `VECTOR_STORE_BACKEND` | `pgvector` | `tfidf` |
| `EMBEDDINGS_BACKEND` | `ollama` | `hash` (deterministic, offline) |

This is what makes the test suite honest: unit tests run against the light
adapters with no infrastructure, and integration tests run against real Postgres,
real pgvector and real Redis, because the bugs worth catching there — wrong SQL,
a forked hash chain, an inverted vector filter — are exactly the ones a mock is
defined not to have.

## The three demo scenarios

The seed data is built so that every branch of the workflow is reachable:

1. **`I need a waterproof hiking boot under $160`** → retrieval, pricing and
   trust verification all pass → a signed `Offer`, then the AP2 mandate chain
   settles it.
2. **`I want the -12C down sleeping bag`** → that SKU's provenance chain is
   deliberately missing its `quality_control` event, so the trust agent refuses
   it with `reason_code: CHAIN_GAP`. The gate blocks an unverifiable product
   rather than rubber-stamping it.
3. **`a titanium spaceship engine under $5`** → nothing matches, the
   validator/repair loop runs to its bounded retry limit and returns a clean
   rejection instead of looping or inventing a product.

Plus a **safety recall**: revoke a credential between offer and payment and
settlement is refused with `CREDENTIAL_REVOKED`. Re-verifying at settlement is
the difference between a system that can stop a bad sale and one that documents
it afterwards. `python scripts/demo.py` walks all of it.

## Running without Docker

Needs PostgreSQL 16 with pgvector, and Redis.

```bash
make install                      # deps + shared library (editable)
createdb agentmarket && psql agentmarket -c 'CREATE EXTENSION vector'
export DATABASE_URL=postgresql://localhost:5432/agentmarket
export EVENT_BUS_BACKEND=redis    # Redis Streams instead of Kafka
make dev-seed                     # schema + demo data + vector index
make dev                          # five services on :8080-:8084
make demo                         # CLI walkthrough
cd ui && npm install && npm run dev
```

Without Ollama running, the planner falls back to a deterministic keyword parser
and embeddings fall back to hashed n-grams. Everything still works; semantic
matching is just less clever, and `/health` reports the storefront as `degraded`
rather than pretending otherwise.

## Tests

```bash
make test        # unit only, no infrastructure required
make test-all    # + integration (Postgres, Redis) and end-to-end (running stack)
```

Integration and end-to-end tests skip themselves when their dependencies are
absent, so the suite is always runnable.

## Layout

```
libs/agentmarket_core/      shared library — the system's actual logic
  config.py                  every tunable and backend selector, one place
  models.py                  strict pydantic contracts for every boundary
  adapters/                  bus, featurestore, productstore, vectorstore, embeddings
  domain/                    pricing, bandit, provenance, ledger, trust_tokens,
                             verification, mandates, payments
  db/schema.sql              the whole schema, applied idempotently at startup
  clients.py                 typed inter-service HTTP clients
  tracing.py                 OTel-shaped spans → Postgres + the bus
services/
  gateway/                   agent identity, routing, ops read models, SSE feed
  pricing/                   Quote API + competitor-signal consumer      (S4)
  storefront/                the LangGraph workflow                      (S5)
  verification/              provenance, trust tokens, ledger            (S6)
  payments/                  AP2 mandates + settlement                   (S6.4)
ui/                          Next.js console + ops dashboard             (S9.1)
infra/                       docker-compose.yml, Dockerfile, postgres init
data/                        demo catalog, competitor feed, EPCIS events
scripts/                     seed.py, demo.py, bootstrap_topics.py, run_local.sh
tests/unit, tests/integration
```

## Design decisions worth knowing

**Guardrails run after the model and can only override it.** A bandit that has
learned something stupid, or a poisoned competitor feed, still cannot produce a
price below cost, below MAP, or identical to a rival's. The learned component is
bounded by rules a human wrote and can read.

**The LLM never produces a fact.** It turns free text into a retrieval intent and
nothing else. Every number and attribute in an offer is copied verbatim from the
pricing engine, product store and verification service, so there is no path by
which a hallucinated price reaches a buyer.

**Hash chain, not blockchain.** What a blockchain buys is tamper-evidence without
a trusted operator. Here the merchant *is* the trusted issuer — the agent trusts
the platform's signature or does not transact. So the consensus half is cost with
no benefit, while the hash-chain half, which is what makes tampering detectable,
is kept: one SHA-256 per write, no gas, no finality wait, and no publishing
commercially sensitive supply-chain data. The legitimate blockchain touchpoint is
x402's stablecoin settlement rail, which is already in the design.

**Everything is re-verified at settlement.** Quotes expire and credentials get
revoked; both are time-varying facts, so the payment orchestrator re-checks the
signature chain, the intent cap, the quote and the trust verdict before money
moves, rather than trusting what the storefront said seconds earlier.

## Production gaps

Honest list of what a real deployment would still need: mTLS and OAuth2
client-credentials at the edge instead of symmetric JWTs; the Ed25519 key in an
HSM/KMS rather than a mounted file; principals' mandate keys held by their own
wallets instead of by the payments service; real payment-processor SDKs behind
`domain/payments.py`; live ERP/MES feeds behind the provenance pipeline; and
Kafka replication factor above 1.
