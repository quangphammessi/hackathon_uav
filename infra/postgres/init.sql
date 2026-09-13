-- Runs once, on first initialization of the Postgres data volume.
-- The application applies its own schema at startup (agentmarket_core.db.migrate);
-- this only guarantees the pgvector extension exists before any service connects.
CREATE EXTENSION IF NOT EXISTS vector;
