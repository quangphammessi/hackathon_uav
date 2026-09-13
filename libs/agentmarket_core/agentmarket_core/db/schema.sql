-- AgentMarket OS -- PostgreSQL schema (proposal §7.3, "structured product
-- store + hash-anchored ledger"). Applied idempotently at service startup by
-- agentmarket_core.db.migrate, so a fresh Compose stack is usable with no
-- manual migration step.

CREATE EXTENSION IF NOT EXISTS vector;

-- ---------------------------------------------------------------------------
-- Subsystem 2 -- structured product graph (source of truth for every
-- factual field in an Offer; the LLM is never allowed to invent these)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS products (
    sku             TEXT PRIMARY KEY,
    gtin            TEXT NOT NULL,
    name            TEXT NOT NULL,
    category        TEXT NOT NULL,
    description     TEXT NOT NULL,
    attributes      JSONB NOT NULL DEFAULT '{}'::jsonb,
    currency        TEXT NOT NULL DEFAULT 'AUD',
    -- commercial fields: never leave the platform, never reach an agent
    internal_cost   NUMERIC(12,2) NOT NULL,
    map_price       NUMERIC(12,2) NOT NULL,
    list_price      NUMERIC(12,2) NOT NULL,
    inventory_units INTEGER NOT NULL DEFAULT 0,
    batch           TEXT,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS products_category_idx ON products (category);
CREATE INDEX IF NOT EXISTS products_gtin_idx ON products (gtin);

-- Values claims the merchant *asserts* about a product ("ethically made",
-- "recycled"). Asserting is free, so nothing downstream is allowed to treat
-- this column as evidence: a claim only satisfies a buyer's values constraint
-- once `provenance_events` contains a matching certification event from an
-- independent auditor (see domain/claims.py). Storing the assertion anyway is
-- what makes the gap detectable -- an unattested claim is a finding, not a
-- silence.
ALTER TABLE products ADD COLUMN IF NOT EXISTS claims JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE products ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'core';

-- Product graph edges. Bundling walks these rather than guessing from
-- category, because "what goes with this" is merchandising knowledge, not
-- something a similarity score can recover: a hydration bladder is not
-- semantically similar to a backpack, it is *complementary* to one.
CREATE TABLE IF NOT EXISTS product_relations (
    sku         TEXT NOT NULL REFERENCES products(sku) ON DELETE CASCADE,
    related_sku TEXT NOT NULL REFERENCES products(sku) ON DELETE CASCADE,
    relation    TEXT NOT NULL,          -- complement | alternative
    note        TEXT,
    PRIMARY KEY (sku, related_sku, relation)
);
CREATE INDEX IF NOT EXISTS product_relations_sku_idx ON product_relations (sku, relation);

-- Semantic layer. Dimension is fixed by EMBEDDING_DIMENSIONS so swapping the
-- embedding model never forces a column-type migration.
CREATE TABLE IF NOT EXISTS product_embeddings (
    sku         TEXT PRIMARY KEY REFERENCES products(sku) ON DELETE CASCADE,
    model       TEXT NOT NULL,
    embedding   vector(768) NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Cosine-distance ANN index.
--
-- HNSW rather than ivfflat, and the reason is a correctness one, not a
-- performance preference. An ivfflat index partitions vectors into lists
-- around centroids computed *at build time*, so an ivfflat index created on
-- an empty table -- which is exactly what happens here, since this schema is
-- applied before the catalog is seeded -- has no usable centroids. Queries
-- against it then silently return a subset of the matches, or none at all,
-- and only when the planner happens to choose the index. A retrieval system
-- that intermittently returns nothing is worse than a slow one.
--
-- HNSW builds its graph incrementally as rows are inserted, needs no training
-- pass, and is correct on an empty table. It costs more to build and more
-- memory; for a product catalog that is a trivial price for a query that
-- always returns what it should.
DROP INDEX IF EXISTS product_embeddings_cosine_idx;
CREATE INDEX IF NOT EXISTS product_embeddings_hnsw_idx
    ON product_embeddings USING hnsw (embedding vector_cosine_ops);

-- ---------------------------------------------------------------------------
-- Subsystem 1 -- offline feature store (the online half lives in Redis).
-- Every competitor observation is retained, accepted or not, because the
-- rejections are what you need to audit the outlier filter later (§4.1).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS competitor_observations (
    id          BIGSERIAL PRIMARY KEY,
    sku         TEXT NOT NULL,
    competitor  TEXT NOT NULL,
    price       NUMERIC(12,2) NOT NULL,
    currency    TEXT NOT NULL DEFAULT 'AUD',
    accepted    BOOLEAN NOT NULL,
    reason      TEXT,
    observed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS competitor_obs_sku_time_idx
    ON competitor_observations (sku, observed_at DESC);

CREATE TABLE IF NOT EXISTS quotes (
    quote_id     TEXT PRIMARY KEY,
    sku          TEXT NOT NULL,
    amount       NUMERIC(12,2) NOT NULL,
    currency     TEXT NOT NULL DEFAULT 'AUD',
    spread       NUMERIC(8,4) NOT NULL,
    fair_value   NUMERIC(12,2) NOT NULL,
    guardrails   JSONB NOT NULL DEFAULT '[]'::jsonb,
    issued_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_until  TIMESTAMPTZ NOT NULL,
    outcome      TEXT,                      -- 'won' | 'lost' | NULL (still open)
    outcome_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS quotes_sku_idx ON quotes (sku, issued_at DESC);

-- Contextual-bandit state for the spread tuner (§4.2). Persisted so the
-- learned policy survives a restart instead of resetting every deploy.
CREATE TABLE IF NOT EXISTS bandit_arms (
    sku         TEXT NOT NULL,
    spread      NUMERIC(8,4) NOT NULL,
    pulls       INTEGER NOT NULL DEFAULT 0,
    reward_sum  NUMERIC(12,4) NOT NULL DEFAULT 0,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (sku, spread)
);

-- ---------------------------------------------------------------------------
-- Subsystem 3 -- provenance + trust tokens + hash-anchored ledger (§6)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS provenance_events (
    id          BIGSERIAL PRIMARY KEY,
    sku         TEXT NOT NULL,
    batch       TEXT NOT NULL,
    event_type  TEXT NOT NULL,          -- ObjectEvent | AggregationEvent | TransactionEvent
    biz_step    TEXT NOT NULL,          -- manufacturing | quality_control | packaging | shipping
    location    TEXT NOT NULL,
    actor       TEXT NOT NULL,
    event_time  TIMESTAMPTZ NOT NULL,
    note        TEXT,
    UNIQUE (sku, batch, biz_step, event_time)
);
CREATE INDEX IF NOT EXISTS provenance_sku_batch_idx ON provenance_events (sku, batch);

CREATE TABLE IF NOT EXISTS trust_tokens (
    token_id          TEXT PRIMARY KEY,
    sku               TEXT NOT NULL,
    batch             TEXT NOT NULL,
    gs1_digital_link  TEXT NOT NULL,
    issuer            TEXT NOT NULL,
    issuance_date     TIMESTAMPTZ NOT NULL,
    event_chain_hash  TEXT NOT NULL,
    credential        JSONB NOT NULL,     -- the full VC-shaped document
    revoked           BOOLEAN NOT NULL DEFAULT FALSE,
    revoked_reason    TEXT,
    revoked_at        TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS trust_tokens_sku_idx ON trust_tokens (sku, revoked);
CREATE INDEX IF NOT EXISTS trust_tokens_link_idx ON trust_tokens (gs1_digital_link);

-- Append-only hash chain. `seq` ordering + prev_hash linkage is what makes
-- tamper-evidence *checkable* (verify_chain_integrity recomputes every row),
-- which is the property the proposal wanted without paying for a blockchain.
CREATE TABLE IF NOT EXISTS trust_ledger (
    seq         BIGSERIAL PRIMARY KEY,
    event_type  TEXT NOT NULL,          -- ISSUE | REVOKE
    token_id    TEXT NOT NULL,
    payload     JSONB NOT NULL,
    prev_hash   TEXT NOT NULL,
    entry_hash  TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS trust_ledger_token_idx ON trust_ledger (token_id, seq DESC);

-- ---------------------------------------------------------------------------
-- Payments (§6.4) -- mandates are retained because AP2's whole point is a
-- non-repudiable, auditable authorization chain.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS orders (
    order_id       TEXT PRIMARY KEY,
    sku            TEXT NOT NULL,
    amount         NUMERIC(12,2) NOT NULL,
    currency       TEXT NOT NULL DEFAULT 'AUD',
    rail           TEXT,
    settlement_ref TEXT,
    status         TEXT NOT NULL,       -- SETTLED | REJECTED
    reason_code    TEXT,
    agent_id       TEXT,
    principal_id   TEXT,
    intent_mandate JSONB,
    cart_mandate   JSONB,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS orders_created_idx ON orders (created_at DESC);

-- ---------------------------------------------------------------------------
-- Agent-to-agent negotiation. Persisted rather than held in memory for the
-- same reason quotes are: the buyer's agent may counter against a different
-- replica than the one that made the opening offer, and every concession has
-- to be reconstructable afterwards -- "why did we sell at that price" is an
-- audit question, not a log line.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS negotiations (
    negotiation_id TEXT PRIMARY KEY,
    agent_id       TEXT NOT NULL,
    sku            TEXT NOT NULL,
    opening_amount NUMERIC(12,2) NOT NULL,
    current_amount NUMERIC(12,2) NOT NULL,
    current_quote  TEXT,
    status         TEXT NOT NULL,       -- OPEN | CONCEDED | ALTERNATIVE | HELD | EXHAUSTED | ACCEPTED
    rounds         JSONB NOT NULL DEFAULT '[]'::jsonb,
    -- The decoded intent and the candidate set from the opening offer. A
    -- counter-offer has to be answered against what the buyer originally
    -- asked for, and the round may land on a different replica than the one
    -- that made the offer, so the context travels with the negotiation rather
    -- than living in the process that started it.
    context        JSONB NOT NULL DEFAULT '{}'::jsonb,
    opened_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS negotiations_agent_idx ON negotiations (agent_id, opened_at DESC);

-- ---------------------------------------------------------------------------
-- Observability (§5.5)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS trace_spans (
    span_id     TEXT PRIMARY KEY,
    trace_id    TEXT NOT NULL,
    name        TEXT NOT NULL,
    service     TEXT NOT NULL,
    started_at  DOUBLE PRECISION NOT NULL,
    ended_at    DOUBLE PRECISION NOT NULL,
    duration_ms DOUBLE PRECISION NOT NULL,
    status      TEXT NOT NULL DEFAULT 'OK',
    attributes  JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS trace_spans_trace_idx ON trace_spans (trace_id, started_at);
CREATE INDEX IF NOT EXISTS trace_spans_created_idx ON trace_spans (created_at DESC);
