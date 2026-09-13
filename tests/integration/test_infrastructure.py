"""Integration tests against real PostgreSQL + pgvector and real Redis.

These exercise the things a mock cannot fail on: SQL that is subtly wrong, a
hash chain that does not hold under a concurrent append, a vector filter
applied in the wrong direction, a Redis pipeline that loses a field. Skipped
automatically when the services are not reachable.
"""
from __future__ import annotations

import json
import threading

import pytest

from tests.conftest import TEST_SKU, requires_postgres, requires_redis

pytestmark = pytest.mark.integration

SLEEPING_BAG = "0950600013480"  # deliberately missing its quality_control event


@requires_postgres
class TestSchema:
    def test_migrate_is_idempotent(self, db_schema):
        db_schema.migrate()
        db_schema.migrate()
        row = db_schema.query_one(
            "SELECT count(*) AS n FROM information_schema.tables "
            "WHERE table_schema='public' AND table_name='products'"
        )
        assert row["n"] == 1

    def test_pgvector_extension_is_installed(self, db_schema):
        row = db_schema.query_one("SELECT extversion FROM pg_extension WHERE extname='vector'")
        assert row is not None


@requires_postgres
class TestVectorSearch:
    @pytest.fixture
    def indexed(self, seeded_db):
        from agentmarket_core.adapters.embeddings.hashing import HashingEmbedder
        from agentmarket_core.adapters.productstore.postgres_store import PostgresProductStore
        from agentmarket_core.adapters.vectorstore.pgvector_store import PgVectorStore

        store = PgVectorStore(embedder=HashingEmbedder())
        store.index(PostgresProductStore().searchable_documents())
        return store

    def test_indexes_every_product(self, indexed, seeded_db):
        row = seeded_db.query_one("SELECT count(*) AS n FROM product_embeddings")
        assert row["n"] == 8

    def test_finds_the_right_product_for_a_natural_query(self, indexed):
        hits = indexed.search("waterproof hiking boot", top_k=3)
        assert hits and hits[0][0] == TEST_SKU

    def test_scores_are_similarities_not_distances(self, indexed):
        """Ranking must improve with relevance -- an inverted operator here is
        the classic pgvector bug and would silently return the worst match."""
        hits = indexed.search("waterproof hiking boot", top_k=5)
        scores = [s for _, s in hits]
        assert scores == sorted(scores, reverse=True)
        assert all(-1.0 <= s <= 1.0 for s in scores)

    def test_price_filter_is_applied_in_sql(self, indexed, seeded_db):
        expensive = seeded_db.query_one(
            "SELECT list_price::float AS p FROM products WHERE sku = %s", (SLEEPING_BAG,)
        )["p"]
        hits = indexed.search("down sleeping bag for freezing weather", top_k=5,
                              max_price=expensive - 1)
        assert SLEEPING_BAG not in [sku for sku, _ in hits]

    def test_ann_index_returns_full_recall_when_the_planner_uses_it(self, indexed, seeded_db):
        """Regression: the schema is applied before any rows are seeded, so an
        ivfflat index here would be built with no centroids and would silently
        return a subset of matches -- but only when the planner happened to
        choose it, which made it look intermittent. `enable_seqscan = off`
        forces the index path so the failure is deterministic.
        """
        with seeded_db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SET enable_seqscan = off")
                cur.execute("SELECT embedding FROM product_embeddings WHERE sku = %s", (TEST_SKU,))
                probe = cur.fetchone()["embedding"]
                cur.execute(
                    "SELECT sku FROM product_embeddings ORDER BY embedding <=> %s LIMIT 8",
                    (probe,),
                )
                rows = cur.fetchall()
        total = seeded_db.query_one("SELECT count(*) AS n FROM product_embeddings")["n"]
        assert len(rows) == total, "ANN index dropped rows -- is it ivfflat built on an empty table?"

    def test_out_of_stock_products_are_excluded(self, indexed, seeded_db):
        seeded_db.execute("UPDATE products SET inventory_units = 0 WHERE sku = %s", (TEST_SKU,))
        hits = indexed.search("waterproof hiking boot", top_k=5)
        assert TEST_SKU not in [sku for sku, _ in hits]


@requires_postgres
class TestProvenance:
    def test_complete_chain_is_ready(self, seeded_db):
        from agentmarket_core.domain import provenance

        status = provenance.chain_status(TEST_SKU)
        assert status["ready"] is True
        assert status["missing_steps"] == []
        assert status["event_chain_hash"]

    def test_incomplete_chain_is_reported_as_a_gap(self, seeded_db):
        from agentmarket_core.domain import provenance

        status = provenance.chain_status(SLEEPING_BAG)
        assert status["ready"] is False
        assert "quality_control" in status["missing_steps"]
        assert status["reason_code"] == "CHAIN_GAP"

    def test_chain_hash_changes_if_an_event_is_altered(self, seeded_db):
        """This is what binds an issued credential to the events that
        justified it -- back-dating an event must be detectable."""
        from agentmarket_core.domain import provenance

        before = provenance.chain_status(TEST_SKU)["event_chain_hash"]
        seeded_db.execute(
            "UPDATE provenance_events SET location = 'somewhere else' "
            "WHERE sku = %s AND biz_step = 'manufacturing'",
            (TEST_SKU,),
        )
        assert provenance.chain_status(TEST_SKU)["event_chain_hash"] != before


@requires_postgres
class TestLedgerAndTrustTokens:
    @pytest.fixture
    def tokens(self, seeded_db):
        from agentmarket_core.domain.ledger import TrustLedger
        from agentmarket_core.domain.trust_tokens import TrustTokenService

        return TrustTokenService(ledger=TrustLedger(), keys_path=None)

    def test_issues_for_a_complete_chain(self, tokens):
        token, reason = tokens.issue(TEST_SKU)
        assert reason is None
        assert token is not None
        assert token.proof["proofValue"]

    def test_refuses_when_the_chain_has_a_gap(self, tokens):
        token, reason = tokens.issue(SLEEPING_BAG)
        assert token is None
        assert reason == "CHAIN_GAP"

    def test_issued_credential_verifies(self, tokens):
        from agentmarket_core.domain.verification import VerificationService

        token, _ = tokens.issue(TEST_SKU)
        result = VerificationService(token_service=tokens).verify(token.token_id)
        assert result.status == "PASS"

    def test_revoked_credential_fails_verification(self, tokens):
        from agentmarket_core.domain.verification import VerificationService

        token, _ = tokens.issue(TEST_SKU)
        tokens.revoke(token.token_id, "safety recall")
        result = VerificationService(token_service=tokens).verify(token.token_id)
        assert result.status == "FAIL"
        assert result.reason_code == "CREDENTIAL_REVOKED"

    def test_unknown_token_fails_verification(self, tokens):
        from agentmarket_core.domain.verification import VerificationService

        result = VerificationService(token_service=tokens).verify("vc_never_issued")
        assert result.reason_code == "TOKEN_NOT_FOUND"

    def test_editing_provenance_after_issuance_fails_verification(self, tokens, seeded_db):
        """A valid signature only proves the credential is unmodified. This
        catches the other direction: the events underneath it changing."""
        from agentmarket_core.domain.verification import VerificationService

        token, _ = tokens.issue(TEST_SKU)
        seeded_db.execute(
            "UPDATE provenance_events SET actor = 'someone else' "
            "WHERE sku = %s AND biz_step = 'packaging'",
            (TEST_SKU,),
        )
        result = VerificationService(token_service=tokens).verify(token.token_id)
        assert result.status == "FAIL"
        assert result.reason_code == "CHAIN_HASH_MISMATCH"

    def test_a_second_signing_key_cannot_verify_the_first_key_s_token(self, tokens, seeded_db):
        """Why the signing key is persisted: an ephemeral per-process key makes
        every credential unverifiable by every other replica."""
        from agentmarket_core.domain.ledger import TrustLedger
        from agentmarket_core.domain.trust_tokens import TrustTokenService
        from agentmarket_core.domain.verification import VerificationService

        token, _ = tokens.issue(TEST_SKU)
        other = TrustTokenService(ledger=TrustLedger(), keys_path=None)
        result = VerificationService(token_service=other).verify(token.token_id)
        assert result.reason_code == "SIGNATURE_INVALID"

    def test_chain_is_intact_after_issuance_and_revocation(self, tokens, seeded_db):
        from agentmarket_core.domain.ledger import trust_ledger

        token, _ = tokens.issue(TEST_SKU)
        tokens.revoke(token.token_id, "recall")
        ok, error = trust_ledger.verify_chain_integrity()
        assert ok is True and error is None

    def test_tampering_with_a_ledger_row_is_detected(self, tokens, seeded_db):
        from agentmarket_core.domain.ledger import trust_ledger

        tokens.issue(TEST_SKU)
        seeded_db.execute(
            "UPDATE trust_ledger SET payload = jsonb_set(payload, '{sku}', '\"tampered\"') "
            "WHERE seq = (SELECT min(seq) FROM trust_ledger)"
        )
        ok, error = trust_ledger.verify_chain_integrity()
        assert ok is False and "tampered payload" in error

    def test_deleting_a_ledger_row_breaks_the_links(self, tokens, seeded_db):
        from agentmarket_core.domain.ledger import trust_ledger

        token, _ = tokens.issue(TEST_SKU)
        tokens.revoke(token.token_id, "recall")
        seeded_db.execute("DELETE FROM trust_ledger WHERE seq = (SELECT min(seq) FROM trust_ledger)")
        ok, error = trust_ledger.verify_chain_integrity()
        assert ok is False and "broken link" in error

    def test_concurrent_appends_do_not_fork_the_chain(self, seeded_db):
        """Ten threads appending at once must still produce one linear chain --
        this is what the SELECT ... FOR UPDATE on the tail is for."""
        from agentmarket_core.domain.ledger import TrustLedger

        ledger = TrustLedger()
        errors: list[Exception] = []

        def append(i: int) -> None:
            try:
                ledger.append("ISSUE", f"vc_{i}", {"i": i})
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=append, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, errors
        ok, error = ledger.verify_chain_integrity()
        assert ok is True, error
        assert seeded_db.query_one("SELECT count(*) AS n FROM trust_ledger")["n"] == 10


@requires_redis
class TestRedisFeatureStore:
    @pytest.fixture
    def store(self, redis_prefix):
        from agentmarket_core.adapters.featurestore.redis_store import RedisFeatureStore
        from agentmarket_core.config import settings

        store = RedisFeatureStore(url=settings.redis_url)
        # Namespace this test's keys so parallel tests cannot collide.
        store._fkey = staticmethod(lambda sku: f"{redis_prefix}:features:{sku}")  # type: ignore[method-assign]
        store._ckey = staticmethod(lambda sku: f"{redis_prefix}:competitors:{sku}")  # type: ignore[method-assign]
        return store

    def test_scalar_features_round_trip_with_their_types(self, store):
        store.write(TEST_SKU, internal_cost=62.0, inventory_units=10, batch="B-1")
        features = store.get_online(TEST_SKU)
        assert features["internal_cost"] == 62.0
        assert features["inventory_units"] == 10
        assert features["batch"] == "B-1"

    def test_one_price_is_kept_per_competitor(self, store):
        store.record_competitor_price(TEST_SKU, "RivalCo", 140.0, True, None)
        store.record_competitor_price(TEST_SKU, "RivalCo", 135.0, True, None)
        assert store.competitor_prices(TEST_SKU) == [135.0]

    def test_outlier_filter_works_against_real_redis(self, store):
        store.ingest_competitor_price(TEST_SKU, "a", 142.0)
        store.ingest_competitor_price(TEST_SKU, "b", 138.5)
        accepted, reason = store.ingest_competitor_price(TEST_SKU, "OutdoorHub", 6.00)
        assert accepted is False and reason
        assert 6.00 not in store.competitor_prices(TEST_SKU)


@requires_redis
class TestRedisStreamsBus:
    def test_events_cross_a_real_broker_between_two_bus_instances(self, redis_prefix):
        """Publisher and consumer are separate objects with separate
        connections, so this genuinely exercises the network path."""
        import time

        from agentmarket_core.adapters.bus.redis_streams import RedisStreamsEventBus
        from agentmarket_core.config import settings

        topic = f"{redis_prefix}.topic"
        received: list[dict] = []

        consumer = RedisStreamsEventBus(settings.redis_url, "consumer", f"{redis_prefix}-group")
        consumer.subscribe(topic, received.append)
        consumer.start()
        try:
            publisher = RedisStreamsEventBus(settings.redis_url, "publisher", f"{redis_prefix}-pub")
            publisher.publish(topic, {"sku": "X", "n": 1})

            deadline = time.time() + 10
            while time.time() < deadline and not received:
                time.sleep(0.1)
        finally:
            consumer.stop()

        assert received, "event did not arrive through Redis Streams within 10s"
        assert received[0]["payload"] == {"sku": "X", "n": 1}
