# AgentMarket OS

A B2A (business-to-agent) commerce platform: an e-commerce backend built to sell
to autonomous AI shopping agents rather than to people. Built for the UAVS
Hackathon 2026, FPT Australasia challenge *"The B2A Shift: Adapting Retail for AI
Shopping Agents"*, implementing the architecture in the accompanying proposal.

Three subsystems over a shared event backbone, an agent-facing gateway, and an
operations UI:

1. **Algorithmic Pricing Engine** (§4) — market-making-style pricing off a live
   competitor feed, with a learned spread, deterministic guardrails, kit
   pricing and a concession policy for negotiating agents.
2. **Agentic RAG Storefront** (§5) — intent decoding, hybrid retrieval and a
   LangGraph workflow that emits machine-readable offers with a justification
   checked against verified facts.
3. **Deterministic Verification Pipeline** (§6) — GS1 EPCIS provenance →
   Ed25519-signed, VC-shaped trust tokens in a hash-anchored ledger → a
   pass/fail gate that payment cannot bypass, and the evidence behind every
   values claim.

```
docker compose -f infra/docker-compose.yml up --build     # or: make up
```

Then open **http://localhost:3000** (Agent Demo Console) and
**http://localhost:3000/ops** (Ops Dashboard). Gateway API docs are at
http://localhost:8080/docs.

First start pulls the Ollama models (~2 GB) before the services come up.

### What a buyer's agent gets back

The challenge asks a merchant system to receive a complex intention, decode it,
evaluate the catalog against it, return a justified proposal, and close through
an API. One request does all five:

```jsonc
POST /v1/query
{ "query": "My dad is turning 60 and wants to start hiking. He has never done it
            before, he gets cold easily, and I only want brands that can
            actually prove they're ethically made. Budget is around $400." }
```

```jsonc
{
  "intent": {                          // 1-2. the decode, returned for audit
    "interpreted_need": "A complete kit beginner-level for the buyer's dad for
                         cold weather restricted to independently verified
                         ethical labour within $400.",
    "constraints": [
      { "field": "experience_level", "op": "lte", "value": "beginner",
        "kind": "hard", "source_phrase": "never done it before" },
      { "field": "claim", "op": "eq", "value": "ethical_labour",
        "kind": "hard", "source_phrase": "prove they're ethically made" }
      // ... each predicate carries the words that produced it
    ]
  },
  "bundle": {                          // 3. a kit composed for this request
    "items": [ { "name": "StepEasy Trail Shoe", "role_in_bundle": "the core item
                 this request is about", "trust_status": "PASS" }, ... ],
    "total": 325.41,
    "dropped": [ { "name": "TrailMedic First-Aid Kit",
                   "reason": "independently verified ethical labour" } ]
  },
  "rationale": {                       // 4. the justification, fact-checked
    "matched": [ { "requirement": "independently verified ethical labour",
                   "evidence": "attested by Fair Labor Association
                                (certificate FLA-2026-7256)" } ],
    "tradeoffs": [ "suitable for cold weather: not met (...)" ],
    "grounding": { "status": "VERIFIED", "violations": [] }
  },
  "negotiation_id": "neg_8054378a14c5" // 5. counter, then pay via AP2 mandates
}
```

---

## What actually runs

| Component | Technology | Notes |
|---|---|---|
| Event backbone | **Kafka** (KRaft, no ZooKeeper) | Keyed by business entity so per-SKU ordering holds; manual offset commits for at-least-once |
| Feature store | **Redis** (online) + **Postgres** (offline) | Hot path never touches disk; every observation retained for audit |
| Product graph | **PostgreSQL 16** | Public and commercial columns split at the type level |
| Vector search | **pgvector** (cosine, HNSW) | Semantic ranking and hard predicates in one SQL statement; HNSW because ivfflat built on an empty table silently drops matches |
| Embeddings | **Ollama** `nomic-embed-text` | Local; deterministic hashed-n-gram fallback when absent |
| Agent orchestration | **LangGraph** | The real graph from proposal Figure 3 |
| Intent decoding | rule layer + **Ollama** `llama3.2` | Deterministic rules are the floor; the model widens coverage and cannot override them |
| Justification | template + checked LLM prose | Every number and values word verified against a fact sheet before it ships |
| Negotiation | concession policy in the pricing service | Bounded by each item's floor and by a cap on total movement |
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

## The demo scenarios

The seed data is built so that every branch is reachable. `python scripts/demo.py`
walks all of them over real HTTP through the gateway; `--only N` runs one.

1. **A complex intention.** *"My dad is turning 60 and wants to start hiking. He
   has never done it before, he gets cold easily, and I only want brands that
   can actually prove they're ethically made. Budget is around $400 for the
   whole kit."* → decoded into predicates, matched, composed into a three-item
   kit, justified requirement by requirement.

   The product that wins is reached through the *structured* retrieval leg, not
   the semantic one: "start hiking" and "designed for first-time hikers" share
   no tokens, so the embedding ranks it below a first-aid kit. That is the
   failure hybrid retrieval exists to prevent.

2. **Negotiation.** The buyer's agent counters. The merchant concedes within its
   floors, and when it cannot, removes the component that earned its place by
   the narrowest margin and offers both options. No response contains cost,
   MAP or margin.

3. **Values that are proven, not asserted.** Two products claim recycled
   materials. One has a Control Union certification event in its provenance
   chain; the other has nothing. A buyer who asked for proof is only offered
   the first, and the second appears in `rejected_alternatives` with the reason.

4. **Refusals that are useful.** *"a sleeping bag rated to -12C for alpine
   conditions"* → the only product meeting the spec has a provenance gap, so it
   is refused with `CHAIN_GAP` and an explanation. *"titanium spaceship engine
   under $5"* → `NO_CANDIDATES` after a bounded repair loop, with the decode
   attached so the agent can see what was understood.

5. **A recall mid-transaction.** Revoke a credential between offer and payment
   and settlement is refused with `CREDENTIAL_REVOKED`. Re-verifying at
   settlement is the difference between a system that can stop a bad sale and
   one that documents it afterwards.

## How it maps to the challenge

The brief offers three illustrative directions and says teams may define their
own approach. This system implements all three, because they turn out to be the
same system viewed from three angles -- and the subsystem that answers each one
already existed in the architecture for a different reason.

| The brief asks for | Here | Where |
|---|---|---|
| **Semantic Intention-Matching Engine** — map product features to *human outcomes*, evaluate the catalogue dynamically, pitch the ideal bundle | Outcome→attribute decoding, hybrid retrieval, per-predicate scoring with evidence, coverage-driven kit composition | `domain/intent.py`, `retrieval.py`, `matching.py`, `bundling.py` |
| **Values-Based "SEO" for Agents** — surface unstructured values so "only buy from ethical brands" matches and *validates* | Claims resolved against auditor certification events in the GS1/EPCIS chain and signed into the trust credential; unattested assertions reported, not dropped | `domain/claims.py`, `trust_tokens.py`, `GET /v1/claims/{sku}` |
| **Dynamic B2A Negotiation Protocol** — the retailer's AI negotiates with the buyer's AI, offering a discount or a model that fits the constraint | Four moves (concede, concede partially, propose an alternative, restructure the kit) bounded by per-item floors and a concession cap, with cost and MAP never crossing the wire | `domain/negotiation.py`, `services/storefront/negotiate.py`, `POST /v1/negotiate` |

Against the in-scope list: B2A marketing strategy is the values-attestation
layer; semantic mapping and intent decoding is the storefront; dynamic bundling
and AI-to-AI negotiation are above; machine-readable data structuring is the
strict pydantic contract on every boundary; API-based checkout is the AP2
mandate chain with per-line re-validation.

The one thing deliberately *not* built is the consumer-facing shopping
assistant. The brief puts it out of scope, and the buyer's agent here is a test
client, not a product.

## Running without Docker

Needs PostgreSQL 16 with pgvector, and Redis. If both are installed but not
running, `./scripts/dev_infra.sh start` brings them up and creates the role,
database and extension idempotently.

```bash
make install                      # deps + shared library (editable)
./scripts/dev_infra.sh start      # or bring up Postgres + Redis yourself
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

227 tests in three tiers: 142 unit (no infrastructure), 39 integration against
real Postgres/pgvector/Redis, 46 end-to-end driving all five services over HTTP
through the gateway. Integration and end-to-end tests skip themselves when their
dependencies are absent, so the suite is always runnable.

The tests that matter most are the ones that try to break the system's own
promises: a composer that hallucinates a price, a values claim asserted with no
attestation behind it, a bundle discount that would breach a component's floor,
a counter-offer at $1, a cart whose total exceeds its lines, and a credential
revoked between the offer and the payment.

## Layout

```
libs/agentmarket_core/      shared library — the system's actual logic
  config.py                  every tunable and backend selector, one place
  models.py                  strict pydantic contracts for every boundary
  adapters/                  bus, featurestore, productstore, vectorstore, embeddings
  domain/
    intent.py                free text -> predicates over catalog fields
    retrieval.py             hybrid: semantic recall + a structured leg
    matching.py              scores candidates, keeps the evidence
    claims.py                values claims vs. what provenance attests
    rationale.py             the justification and its grounding check
    bundling.py              kit selection (storefront) + kit pricing (pricing)
    negotiation.py           concession policy and negotiation state
    pricing.py, bandit.py, provenance.py, ledger.py, trust_tokens.py,
    verification.py, mandates.py, payments.py
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

**The LLM never produces a fact, and its prose is checked anyway.** It decodes
queries and writes justifications; every number and attribute in an offer is
copied verbatim from the pricing engine, product store and verification service.
Then the justification it wrote is machine-checked against a fact sheet built
from those same verified sources: every number must appear there, and no values
word may be used unless the provenance chain attests that claim. On any
violation the model's text is discarded, a deterministic template ships, and
`rationale.grounding` tells the buyer which one they got and what failed. The
worst case is a plainer sentence, never a false one.

**Retrieval has two legs because they fail differently.** An embedding search
can miss a product that satisfies every stated requirement -- "start hiking" and
"designed for first-time hikers" are the same need and not the same tokens. A
structured query over the decoded predicates cannot miss it, because it is
asking the question the buyer actually asked. The structured leg alone only
knows the predicates that were decoded; the embedding is what surfaces the
product nobody wrote a rule for. The union has the weaknesses of neither, and
the demo query is a case where the semantic leg alone gets it wrong.

**A values claim is only worth what attests it.** `sustainable: true` in a
catalog is worthless: every merchant sets it. So a claim is resolved against
certification events from independent auditors in the product's GS1/EPCIS
chain, and the resolved set is signed into the trust credential -- which means a
verified claim inherits the credential's properties: it breaks if an event is
back-dated, and it dies when the credential is revoked. A claim the merchant
asserts with nothing behind it is reported as `ASSERTED_UNATTESTED` rather than
dropped, because for a buyer who asked for proof, the difference between those
two states is the entire question.

**Negotiation is bounded on both sides.** The floor (cost plus minimum margin,
and MAP) is absolute and applies inside bundles too, so a kit discount can never
become the route by which a component is sold below the price it would have had
alone. A second bound caps how far any one negotiation may travel from the
opening ask -- without it, the optimal strategy for every buyer's agent is to
counter at $1, and an automated counterparty discovers that immediately. What
crosses the wire is a price and a reason code; cost and MAP are used to compute
the answer inside the pricing service and never leave it.

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
