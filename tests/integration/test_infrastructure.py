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
        """Every catalog row gets an embedding -- asserted against the catalog
        rather than a hard-coded count, so adding a product cannot make the
        suite fail for a reason that has nothing to do with the code."""
        embedded = seeded_db.query_one("SELECT count(*) AS n FROM product_embeddings")["n"]
        products = seeded_db.query_one("SELECT count(*) AS n FROM products")["n"]
        assert embedded == products > 0

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
                cur.execute("SELECT count(*) AS n FROM product_embeddings")
                total = cur.fetchone()["n"]
                # The limit is the whole indexed set, so any row the index
                # fails to return is a dropped match rather than a truncation.
                cur.execute(
                    "SELECT sku FROM product_embeddings ORDER BY embedding <=> %s LIMIT %s",
                    (probe, total),
                )
                rows = cur.fetchall()
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


@requires_postgres
class TestStructuredRetrieval:
    """The SQL half of hybrid retrieval.

    This is the leg that exists so a product satisfying every stated
    requirement cannot be missed, which makes its correctness the whole point.
    It is also real SQL over jsonb containment operators, so it is exactly the
    kind of thing a mocked store would let through broken.
    """

    def test_filters_on_experience_level(self, seeded_db):
        from agentmarket_core.adapters.productstore.postgres_store import PostgresProductStore

        skus = PostgresProductStore().structured_candidates(experience_levels=["beginner"], limit=50)
        assert skus
        for sku in skus:
            row = seeded_db.query_one("SELECT attributes FROM products WHERE sku = %s", (sku,))
            assert row["attributes"]["experience_level"] == "beginner"

    def test_filters_on_a_jsonb_array_of_use_cases(self, seeded_db):
        from agentmarket_core.adapters.productstore.postgres_store import PostgresProductStore

        skus = PostgresProductStore().structured_candidates(use_cases=["cold_weather"], limit=50)
        assert skus
        for sku in skus:
            row = seeded_db.query_one("SELECT attributes FROM products WHERE sku = %s", (sku,))
            assert "cold_weather" in row["attributes"]["use_cases"]

    def test_filters_on_asserted_claims(self, seeded_db):
        from agentmarket_core.adapters.productstore.postgres_store import PostgresProductStore

        skus = PostgresProductStore().structured_candidates(claims=["ethical_labour"], limit=50)
        assert skus
        for sku in skus:
            row = seeded_db.query_one("SELECT claims FROM products WHERE sku = %s", (sku,))
            assert "ethical_labour" in row["claims"]

    def test_filters_compose_as_an_and(self, seeded_db):
        from agentmarket_core.adapters.productstore.postgres_store import PostgresProductStore

        store = PostgresProductStore()
        both = set(store.structured_candidates(
            experience_levels=["beginner"], claims=["ethical_labour"], limit=50))
        assert both <= set(store.structured_candidates(experience_levels=["beginner"], limit=50))
        assert both <= set(store.structured_candidates(claims=["ethical_labour"], limit=50))

    def test_no_filters_returns_the_in_stock_catalog(self, seeded_db):
        from agentmarket_core.adapters.productstore.postgres_store import PostgresProductStore

        seeded_db.execute("UPDATE products SET inventory_units = 0 WHERE sku = %s", (TEST_SKU,))
        skus = PostgresProductStore().structured_candidates(limit=50)
        assert TEST_SKU not in skus

    def test_a_price_ceiling_is_applied_in_sql(self, seeded_db):
        from agentmarket_core.adapters.productstore.postgres_store import PostgresProductStore

        skus = PostgresProductStore().structured_candidates(max_price=60.0, limit=50)
        for sku in skus:
            row = seeded_db.query_one(
                "SELECT list_price::float AS p FROM products WHERE sku = %s", (sku,))
            assert row["p"] <= 60.0


@requires_postgres
class TestProductGraph:
    def test_complements_come_back_from_the_graph(self, seeded_db):
        from agentmarket_core.adapters.productstore.postgres_store import PostgresProductStore

        seeded_db.execute(
            "INSERT INTO product_relations (sku, related_sku, relation) VALUES (%s,%s,'complement')"
            " ON CONFLICT DO NOTHING",
            (TEST_SKU, SLEEPING_BAG),
        )
        assert SLEEPING_BAG in PostgresProductStore().complements_of(TEST_SKU)

    def test_an_edge_to_a_missing_product_is_rejected_by_the_database(self, seeded_db):
        """The graph cannot point at a product that does not exist; a dangling
        complement would put a phantom SKU into a bundle."""
        import psycopg

        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            seeded_db.execute(
                "INSERT INTO product_relations (sku, related_sku, relation)"
                " VALUES (%s,'NOPE','complement')", (TEST_SKU,))


@requires_postgres
class TestValuesClaimsAgainstRealProvenance:
    def test_a_claim_is_verified_from_a_certification_event(self, seeded_db):
        from agentmarket_core.domain import claims as claims_mod
        from agentmarket_core.domain import provenance

        seeded_db.execute(
            """INSERT INTO provenance_events (sku, batch, event_type, biz_step, location,
                                              actor, event_time, note)
               VALUES (%s, %s, 'CertificationEvent', 'certification', 'Audit-1',
                       'Fair Labor Association', '2026-08-03T07:00:00Z',
                       'claim=ethical_labour; certificate=FLA-2026-0001; scope=batch')
               ON CONFLICT DO NOTHING""",
            (TEST_SKU, "B-TP-2026-08"),
        )
        status = provenance.chain_status(TEST_SKU, "B-TP-2026-08")
        results = claims_mod.verify_claims(["ethical_labour"], status["events"], ["ethical_labour"])
        assert results[0].status == "VERIFIED"
        assert results[0].attested_by == "Fair Labor Association"

    def test_the_attestation_is_covered_by_the_event_chain_hash(self, seeded_db):
        """A verified claim has to inherit the credential's tamper-evidence,
        which only holds if the certification event is inside the hash."""
        from agentmarket_core.domain import provenance

        before = provenance.chain_status(TEST_SKU, "B-TP-2026-08")["event_chain_hash"]
        seeded_db.execute(
            """INSERT INTO provenance_events (sku, batch, event_type, biz_step, location,
                                              actor, event_time, note)
               VALUES (%s, %s, 'CertificationEvent', 'certification', 'Audit-9',
                       'Some Other Auditor', '2026-08-04T07:00:00Z',
                       'claim=recycled_materials; certificate=X-1; scope=batch')""",
            (TEST_SKU, "B-TP-2026-08"),
        )
        after = provenance.chain_status(TEST_SKU, "B-TP-2026-08")["event_chain_hash"]
        assert before != after


@requires_postgres
class TestNegotiationPersistence:
    def test_a_negotiation_survives_the_process_that_opened_it(self, db_schema):
        """The buyer's agent may counter against a different replica than the
        one that made the offer."""
        from agentmarket_core.domain.negotiation import NegotiationStore

        opened = NegotiationStore().create("agent-1", TEST_SKU, 149.0, "q_abc",
                                           {"plan": {"raw_query": "x"}})
        reloaded = NegotiationStore().get(opened["negotiation_id"])
        assert reloaded["current_amount"] == 149.0
        assert reloaded["context"]["plan"]["raw_query"] == "x"

    def test_a_restructured_kit_replaces_the_stored_context(self, db_schema):
        from agentmarket_core.domain import negotiation as neg

        store = neg.NegotiationStore()
        opened = store.create("agent-1", TEST_SKU, 300.0, "q1",
                              {"bundle_skus": ["a", "b", "c"]})
        store.append_round(opened["negotiation_id"],
                           [neg.buyer_round(1, 200.0)], "BUNDLE_RESTRUCTURED", 200.0, "q2",
                           context={"bundle_skus": ["a", "b"]})
        reloaded = store.get(opened["negotiation_id"])
        assert reloaded["context"]["bundle_skus"] == ["a", "b"]
        assert reloaded["current_amount"] == 200.0

    def test_omitting_the_context_leaves_it_untouched(self, db_schema):
        from agentmarket_core.domain import negotiation as neg

        store = neg.NegotiationStore()
        opened = store.create("agent-1", TEST_SKU, 300.0, "q1", {"keep": "me"})
        store.append_round(opened["negotiation_id"], [], "HELD", 300.0, "q1")
        assert store.get(opened["negotiation_id"])["context"] == {"keep": "me"}
