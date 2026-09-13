"""Central configuration for AgentMarket OS.

Everything the system can be pointed at -- brokers, databases, model servers,
sibling services -- is read from the environment here and nowhere else, so a
service never hardcodes an address and the same image runs unchanged in
Compose, in CI, and on a laptop (proposal §8.1, secrets & configuration).

The `*_BACKEND` selectors are the important part of this file. Each one picks
a concrete adapter for an abstract port (event bus, feature store, product
store, vector store, embeddings). The production values are the defaults the
proposal describes; the lighter values exist so the whole system still boots
with nothing installed, which is what makes `pytest` and a laptop demo
possible without Kafka. Swapping a backend is a config change, never a code
change -- see `agentmarket_core/adapters/`.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.getenv(name, default)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def _env_int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


REPO_ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class Settings:
    # --- identity of the running process (shows up in traces, events, logs) ---
    service_name: str = field(default_factory=lambda: _env("SERVICE_NAME", "agentmarket"))
    environment: str = field(default_factory=lambda: _env("ENVIRONMENT", "local"))
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO"))

    # --- adapter selection (see module docstring) ---
    event_bus_backend: str = field(default_factory=lambda: _env("EVENT_BUS_BACKEND", "kafka"))
    feature_store_backend: str = field(default_factory=lambda: _env("FEATURE_STORE_BACKEND", "redis"))
    product_store_backend: str = field(default_factory=lambda: _env("PRODUCT_STORE_BACKEND", "postgres"))
    vector_store_backend: str = field(default_factory=lambda: _env("VECTOR_STORE_BACKEND", "pgvector"))
    embeddings_backend: str = field(default_factory=lambda: _env("EMBEDDINGS_BACKEND", "ollama"))

    # --- infrastructure endpoints ---
    database_url: str = field(default_factory=lambda: _env(
        "DATABASE_URL", "postgresql://agentmarket:agentmarket@localhost:5432/agentmarket"))
    redis_url: str = field(default_factory=lambda: _env("REDIS_URL", "redis://localhost:6379/0"))
    kafka_bootstrap_servers: str = field(default_factory=lambda: _env("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092"))
    kafka_consumer_group: str = field(default_factory=lambda: _env("KAFKA_CONSUMER_GROUP", "agentmarket"))

    # --- sibling services (proposal §7.1: services address each other by name) ---
    pricing_url: str = field(default_factory=lambda: _env("PRICING_URL", "http://localhost:8081"))
    storefront_url: str = field(default_factory=lambda: _env("STOREFRONT_URL", "http://localhost:8082"))
    verification_url: str = field(default_factory=lambda: _env("VERIFICATION_URL", "http://localhost:8083"))
    payments_url: str = field(default_factory=lambda: _env("PAYMENTS_URL", "http://localhost:8084"))
    internal_timeout_seconds: float = field(default_factory=lambda: _env_float("INTERNAL_TIMEOUT_SECONDS", 10.0))

    # --- LLM / embeddings (local Ollama; proposal §5.2, §5.4) ---
    ollama_base_url: str = field(default_factory=lambda: _env("OLLAMA_BASE_URL", "http://localhost:11434"))
    ollama_model: str = field(default_factory=lambda: _env("OLLAMA_MODEL", "llama3.2"))
    ollama_embedding_model: str = field(default_factory=lambda: _env("OLLAMA_EMBEDDING_MODEL", "nomic-embed-text"))
    ollama_timeout_seconds: float = field(default_factory=lambda: _env_float("OLLAMA_TIMEOUT_SECONDS", 30.0))
    llm_enabled: bool = field(default_factory=lambda: _env("AGENTMARKET_LLM_PROVIDER", "ollama") != "none")

    # Embeddings are fixed-width regardless of backend so the pgvector column
    # type never has to change when you swap embedding models. A model with a
    # different native width is projected/padded to this size (see adapters).
    embedding_dimensions: int = field(default_factory=lambda: _env_int("EMBEDDING_DIMENSIONS", 768))

    # --- pricing engine (proposal §4) ---
    min_margin_ratio: float = field(default_factory=lambda: _env_float("AGENTMARKET_MIN_MARGIN", 0.12))
    competitor_outlier_deviation: float = field(default_factory=lambda: _env_float("COMPETITOR_OUTLIER_DEVIATION", 0.90))
    quote_ttl_seconds: int = field(default_factory=lambda: _env_int("AGENTMARKET_QUOTE_TTL", 120))
    bandit_epsilon: float = field(default_factory=lambda: _env_float("BANDIT_EPSILON", 0.15))

    # --- verification pipeline (proposal §6) ---
    trust_confidence_threshold: float = field(default_factory=lambda: _env_float("AGENTMARKET_TRUST_THRESHOLD", 0.90))
    issuer_did: str = field(default_factory=lambda: _env("ISSUER_DID", "did:web:agentmarket.example"))
    signing_key_path: str = field(default_factory=lambda: _env("SIGNING_KEY_PATH", "/var/lib/agentmarket/keys.json"))

    # --- payments (proposal §6.4) ---
    micropayment_threshold: float = field(default_factory=lambda: _env_float("AGENTMARKET_MICROPAYMENT_THRESHOLD", 20.00))

    # --- gateway / agent identity (proposal §8.1) ---
    jwt_secret: str = field(default_factory=lambda: _env("AGENTMARKET_JWT_SECRET", "hackathon-demo-secret-do-not-use-in-prod"))
    jwt_algorithm: str = "HS256"
    jwt_ttl_seconds: int = field(default_factory=lambda: _env_int("AGENTMARKET_JWT_TTL", 3600))

    # --- storefront workflow ---
    max_repair_retries: int = field(default_factory=lambda: _env_int("MAX_REPAIR_RETRIES", 2))

    # --- seed data (used by scripts/seed.py to populate Postgres) ---
    data_dir: Path = field(default_factory=lambda: Path(_env("AGENTMARKET_DATA_DIR", str(REPO_ROOT / "data"))))

    cors_allow_origins: str = field(default_factory=lambda: _env("CORS_ALLOW_ORIGINS", "*"))

    @property
    def spread_candidates(self) -> list[float]:
        raw = os.getenv("SPREAD_CANDIDATES", "0.01,0.03,0.05,0.08")
        return [float(x) for x in raw.split(",") if x.strip()]

    @property
    def required_biz_steps(self) -> list[str]:
        raw = os.getenv("REQUIRED_BIZ_STEPS", "manufacturing,quality_control,packaging,shipping")
        return [x.strip() for x in raw.split(",") if x.strip()]


settings = Settings()

# Topic names are part of the inter-service contract (proposal §7.2), so they
# live beside the config rather than being typed as string literals at each
# call site.
TOPIC_COMPETITOR_SIGNAL = "market.competitor_signal"
TOPIC_QUOTE_ISSUED = "pricing.quote_issued"
TOPIC_TRANSACTION_OUTCOME = "pricing.transaction_outcome"
TOPIC_TRUST_ISSUED = "trust.issued"
TOPIC_TRUST_REVOKED = "trust.revoked"
TOPIC_STOREFRONT_OFFER = "storefront.offer"
TOPIC_STOREFRONT_REJECTION = "storefront.rejection"
TOPIC_PAYMENT_SETTLED = "payments.settled"
TOPIC_PAYMENT_REJECTED = "payments.rejected"
TOPIC_TRACE_SPAN = "observability.trace_span"

ALL_TOPICS = [
    TOPIC_COMPETITOR_SIGNAL,
    TOPIC_QUOTE_ISSUED,
    TOPIC_TRANSACTION_OUTCOME,
    TOPIC_TRUST_ISSUED,
    TOPIC_TRUST_REVOKED,
    TOPIC_STOREFRONT_OFFER,
    TOPIC_STOREFRONT_REJECTION,
    TOPIC_PAYMENT_SETTLED,
    TOPIC_PAYMENT_REJECTED,
    TOPIC_TRACE_SPAN,
]
