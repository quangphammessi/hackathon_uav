"""End-to-end journey across all five running services, over real HTTP.

Skipped unless a gateway is reachable (`./scripts/run_local.sh start`, or the
Compose stack). This is the test that would catch a broken inter-service
contract, a missing route, or an auth dependency that stopped firing --
none of which any single-service test can see.
"""
from __future__ import annotations

import os
import urllib.error
import urllib.request

import pytest

pytestmark = pytest.mark.integration

GATEWAY = os.getenv("GATEWAY_URL", "http://127.0.0.1:8080")


def _gateway_up() -> bool:
    try:
        with urllib.request.urlopen(f"{GATEWAY}/live", timeout=2) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


requires_gateway = pytest.mark.skipif(
    not _gateway_up(), reason=f"gateway not reachable at {GATEWAY}"
)


def call(method: str, path: str, body=None, token: str | None = None):
    import json

    req = urllib.request.Request(f"{GATEWAY}{path}", method=method)
    req.add_header("content-type", "application/json")
    if token:
        req.add_header("authorization", f"Bearer {token}")
    data = json.dumps(body).encode() if body is not None else None
    with urllib.request.urlopen(req, data, timeout=60) as r:
        return json.loads(r.read())


@pytest.fixture(scope="module")
def agent():
    return call("POST", "/v1/agents/token", {"agent_name": "IntegrationTestAgent"})


@requires_gateway
class TestAgentIdentity:
    def test_issues_a_usable_token(self, agent):
        assert agent["agent_id"].startswith("agent_")
        assert agent["token"] and agent["expires_in"] > 0

    def test_query_without_a_token_is_rejected(self):
        with pytest.raises(urllib.error.HTTPError) as exc:
            call("POST", "/v1/query", {"query": "anything"})
        assert exc.value.code == 401

    def test_query_with_a_forged_token_is_rejected(self):
        with pytest.raises(urllib.error.HTTPError) as exc:
            call("POST", "/v1/query", {"query": "anything"}, token="not.a.real.token")
        assert exc.value.code == 401


@requires_gateway
class TestStorefrontJourney:
    def test_a_normal_request_produces_a_verified_offer(self, agent):
        resp = call("POST", "/v1/query",
                    {"query": "I need a waterproof hiking boot under $160"}, token=agent["token"])
        assert resp["outcome"] == "OFFER"
        offer = resp["result"]
        assert offer["trust_status"] == "PASS"
        assert offer["price"]["amount"] > 0
        assert offer["trust_token_ref"].startswith("vc_")

    def test_a_provenance_gap_is_refused_with_a_reason_code(self, agent):
        resp = call("POST", "/v1/query",
                    {"query": "I want the -12C down sleeping bag"}, token=agent["token"])
        assert resp["outcome"] == "REJECTED"
        assert resp["result"]["reason_code"] == "CHAIN_GAP"

    def test_an_unsatisfiable_request_terminates_instead_of_looping(self, agent):
        resp = call("POST", "/v1/query",
                    {"query": "a titanium spaceship engine under $5"}, token=agent["token"])
        assert resp["outcome"] == "REJECTED"
        assert resp["result"]["reason_code"] in {"NO_CANDIDATES", "NO_MATCHING_PRODUCT"}

    def test_the_offer_never_leaks_commercial_fields(self, agent):
        """Internal cost and MAP must be structurally absent from anything an
        agent can see -- leaking unit economics is not recoverable."""
        resp = call("POST", "/v1/query",
                    {"query": "waterproof hiking boot"}, token=agent["token"])
        blob = str(resp["result"]).lower()
        assert "internal_cost" not in blob
        assert "map_price" not in blob

    def test_the_catalog_never_leaks_commercial_fields(self):
        blob = str(call("GET", "/v1/catalog")).lower()
        assert "internal_cost" not in blob and "map_price" not in blob

    def test_the_run_is_fully_traced(self, agent):
        resp = call("POST", "/v1/query",
                    {"query": "a lightweight water filter for hiking"}, token=agent["token"])
        spans = call("GET", f"/v1/trace/{resp['trace_id']}")["spans"]
        names = [s["name"] for s in spans]
        assert names[:4] == ["planner", "retriever", "spec_extraction", "schema_validator"]
        assert all(s["duration_ms"] >= 0 for s in spans)


@requires_gateway
class TestPaymentJourney:
    @pytest.fixture
    def offer(self, agent):
        resp = call("POST", "/v1/query",
                    {"query": "I need a waterproof hiking boot under $160"}, token=agent["token"])
        assert resp["outcome"] == "OFFER"
        return resp["result"]

    def _intent(self, agent, cap=500.0):
        return call("POST", "/v1/principals/intent-mandate", {
            "principal_id": "principal_test", "agent_id": agent["agent_id"],
            "instructions": "buy hiking boots", "max_amount": cap,
        })

    def test_settles_a_valid_purchase(self, agent, offer):
        order = call("POST", "/v1/pay", {
            "principal_id": "principal_test", "intent_mandate": self._intent(agent),
            "sku": offer["sku"], "quote_id": offer["price"]["quote_id"],
            "amount": offer["price"]["amount"], "trust_token_ref": offer["trust_token_ref"],
        }, token=agent["token"])
        assert order["status"] == "SETTLED"
        assert order["rail"] in {"card_network", "stablecoin_x402"}
        assert order["settlement_ref"]

    def test_the_signed_mandate_chain_is_retrievable(self, agent, offer):
        order = call("POST", "/v1/pay", {
            "principal_id": "principal_test", "intent_mandate": self._intent(agent),
            "sku": offer["sku"], "quote_id": offer["price"]["quote_id"],
            "amount": offer["price"]["amount"], "trust_token_ref": offer["trust_token_ref"],
        }, token=agent["token"])
        detail = call("GET", f"/v1/orders/{order['order_id']}")
        cart = detail["cart_mandate"]
        assert cart["sku"] == offer["sku"]
        assert cart["quote_id"] == offer["price"]["quote_id"]
        assert cart["signature"]

    def test_refuses_to_pay_above_the_principal_s_cap(self, agent, offer):
        order = call("POST", "/v1/pay", {
            "principal_id": "principal_test", "intent_mandate": self._intent(agent, cap=1.00),
            "sku": offer["sku"], "quote_id": offer["price"]["quote_id"],
            "amount": offer["price"]["amount"], "trust_token_ref": offer["trust_token_ref"],
        }, token=agent["token"])
        assert order["status"] == "REJECTED"
        assert order["reason_code"] == "EXCEEDS_INTENT_MANDATE_CAP"

    def test_refuses_a_price_the_engine_never_quoted(self, agent, offer):
        order = call("POST", "/v1/pay", {
            "principal_id": "principal_test", "intent_mandate": self._intent(agent),
            "sku": offer["sku"], "quote_id": offer["price"]["quote_id"],
            "amount": 1.00, "trust_token_ref": offer["trust_token_ref"],
        }, token=agent["token"])
        assert order["status"] == "REJECTED"
        assert order["reason_code"] == "QUOTE_EXPIRED_OR_MISMATCHED"

    def test_refuses_an_unissued_trust_token(self, agent, offer):
        order = call("POST", "/v1/pay", {
            "principal_id": "principal_test", "intent_mandate": self._intent(agent),
            "sku": offer["sku"], "quote_id": offer["price"]["quote_id"],
            "amount": offer["price"]["amount"], "trust_token_ref": "vc_never_issued",
        }, token=agent["token"])
        assert order["status"] == "REJECTED"
        assert order["reason_code"] == "TOKEN_NOT_FOUND"


@requires_gateway
class TestOpsSurface:
    def test_overview_returns_every_section(self):
        overview = call("GET", "/v1/ops/overview")
        for section in ("market", "provenance", "ledger", "orders", "traces", "services"):
            assert section in overview

    def test_every_service_reports_health(self):
        services = call("GET", "/v1/ops/overview")["services"]
        assert set(services) == {"storefront", "pricing", "verification", "payments"}
        for name, health in services.items():
            assert health.get("status") in {"ok", "degraded"}, name

    def test_ledger_integrity_is_intact(self):
        assert call("GET", "/v1/ops/overview")["ledger"]["intact"] is True

    def test_the_sleeping_bag_is_the_one_with_a_chain_gap(self):
        skus = call("GET", "/v1/ops/overview")["provenance"]["skus"]
        not_ready = [s["sku"] for s in skus if not s["ready"]]
        assert not_ready == ["0950600013480"]
