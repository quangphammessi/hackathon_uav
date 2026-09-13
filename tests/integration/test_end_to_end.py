"""End-to-end journey across all five running services, over real HTTP.

Skipped unless a gateway is reachable (`./scripts/run_local.sh start`, or the
Compose stack). This is the test that would catch a broken inter-service
contract, a missing route, or an auth dependency that stopped firing --
none of which any single-service test can see.
"""
from __future__ import annotations

import json
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
                    {"query": "a sleeping bag rated to -12C for alpine conditions"},
                    token=agent["token"])
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
        assert names[:3] == ["intent_decode", "retrieve", "assess"]
        assert all(s["duration_ms"] >= 0 for s in spans)
        # The decode is recorded in the trace, not just used and discarded:
        # an unauditable decode is indistinguishable from a guess.
        decode = next(s for s in spans if s["name"] == "intent_decode")
        assert decode["attributes"]["outputs"]["constraints"]


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


@requires_gateway
class TestIntentDecoding:
    """The capability the challenge weights first, exercised over real HTTP."""

    COMPLEX = ("My dad is turning 60 and wants to start hiking. He has never done it before, "
               "he gets cold easily, and I only want brands that can actually prove they're "
               "ethically made. Budget is around $400 for the whole kit.")

    @pytest.fixture(scope="class")
    def response(self, agent):
        return call("POST", "/v1/query", {"query": self.COMPLEX}, token=agent["token"])

    def test_a_multi_constraint_request_produces_an_offer(self, response):
        assert response["outcome"] == "OFFER"

    def test_the_decode_is_returned_to_the_buyer(self, response):
        """The buyer's agent can check what the merchant thought it asked for,
        rather than trusting the answer."""
        intent = response["result"]["intent"]
        assert intent["interpreted_need"]
        assert intent["experience_level"] == "beginner"
        assert intent["budget"] == 400.0
        assert intent["bundle_intent"] is True
        assert "ethical_labour" in intent["values"]

    def test_every_constraint_cites_the_words_it_came_from(self, response):
        for constraint in response["result"]["intent"]["constraints"]:
            assert constraint["source_phrase"], constraint["field"]

    def test_experience_level_gates_rather_than_ranks(self, response):
        """An expert product must not be sold to a stated first-timer, however
        well it scores."""
        offer = response["result"]
        skus = [offer["sku"]] + [i["sku"] for i in (offer.get("bundle") or {}).get("items", [])]
        catalog = {p["sku"]: p for p in call("GET", "/v1/catalog")["products"]}
        for sku in skus:
            assert catalog[sku]["attributes"]["experience_level"] == "beginner"

    def test_a_spec_threshold_excludes_a_product_that_misses_it(self, agent):
        resp = call("POST", "/v1/query",
                    {"query": "a sleeping bag rated to -12C for alpine conditions"},
                    token=agent["token"])
        considered = {c["name"]: c for c in resp["result"].get("considered", [])}
        warm = next((c for n, c in considered.items() if "WarmNest" in n), None)
        assert warm is not None and not warm["eligible"]


@requires_gateway
class TestJustification:
    def test_an_offer_explains_itself_requirement_by_requirement(self, agent):
        resp = call("POST", "/v1/query",
                    {"query": "trail shoes for someone who has never hiked before, under $200"},
                    token=agent["token"])
        why = resp["result"]["rationale"]
        assert why["summary"]
        assert why["matched"], "an offer with no evidence is just a SKU"
        for match in why["matched"]:
            assert match["requirement"] and match["evidence"]

    def test_the_justification_reports_how_it_was_grounded(self, agent):
        resp = call("POST", "/v1/query",
                    {"query": "a water filter for day hikes under $60"}, token=agent["token"])
        grounding = resp["result"]["rationale"]["grounding"]
        assert grounding["status"] in {"VERIFIED", "TEMPLATE_FALLBACK", "TEMPLATE_ONLY"}
        if grounding["status"] == "VERIFIED":
            assert not grounding["violations"]

    def test_unmet_preferences_are_disclosed(self, agent):
        resp = call("POST", "/v1/query",
                    {"query": "trail shoes for a beginner who gets cold easily"},
                    token=agent["token"])
        why = resp["result"]["rationale"]
        assert isinstance(why["tradeoffs"], list)

    def test_a_refusal_names_the_binding_constraint(self, agent):
        resp = call("POST", "/v1/query",
                    {"query": "a sleeping bag rated to -12C for alpine conditions"},
                    token=agent["token"])
        result = resp["result"]
        assert result["reason_code"] == "CHAIN_GAP"
        assert "supply-chain" in result["detail"] or "provenance" in result["detail"]
        assert result["considered"], "a refusal should show what was considered"


@requires_gateway
class TestVerifiableValues:
    """"Only buy from ethical brands", answered with evidence."""

    def test_an_attested_claim_verifies(self):
        claims = {c["claim"]: c for c in call("GET", "/v1/claims/0950600013459")["claims"]}
        assert claims["recycled_materials"]["status"] == "VERIFIED"
        assert claims["recycled_materials"]["attested_by"]
        assert claims["recycled_materials"]["certificate"]

    def test_an_asserted_claim_with_no_attestation_is_flagged(self):
        """The greenwashing case. The merchant says it; nothing backs it."""
        claims = {c["claim"]: c for c in call("GET", "/v1/claims/0950600013534")["claims"]}
        assert claims["recycled_materials"]["status"] == "ASSERTED_UNATTESTED"
        assert claims["recycled_materials"]["attested_by"] is None

    def test_a_proof_demand_excludes_the_unattested_product(self, agent):
        resp = call("POST", "/v1/query", {
            "query": ("a waterproof rain jacket for day hikes, only from a brand that can "
                      "prove its recycled-material claim, under $230")},
            token=agent["token"])
        assert resp["outcome"] == "OFFER"
        assert resp["result"]["sku"] != "0950600013534", "the greenwashed product must not win"
        rejected = [a["name"] for a in resp["result"]["rationale"]["rejected_alternatives"]]
        assert any("EcoTrail" in name for name in rejected)

    def test_verified_claims_are_inside_the_signed_credential(self):
        """A claim checked separately, after the fact, would not inherit the
        credential's tamper-evidence or its revocation."""
        token_id = call("POST", "/v1/query", {"query": "trail shoes for a beginner under $200"},
                        token=call("POST", "/v1/agents/token",
                                   {"agent_name": "claims-check"})["token"])["result"]["trust_token_ref"]
        record = call("GET", f"/v1/ops/provenance/0950600013510")
        assert record["ready"] is True
        assert token_id.startswith("vc_")


@requires_gateway
class TestBundling:
    @pytest.fixture(scope="class")
    def bundle(self, agent):
        resp = call("POST", "/v1/query", {
            "query": ("everything a complete beginner needs for a first overnight camping trip, "
                      "he gets cold easily, budget around $450")},
            token=agent["token"])
        assert resp["outcome"] == "OFFER", resp["result"].get("reason_code")
        return resp["result"]["bundle"]

    def test_a_kit_request_returns_several_items(self, bundle):
        assert bundle is not None
        assert len(bundle["items"]) >= 2

    def test_every_item_states_its_job_in_the_kit(self, bundle):
        for item in bundle["items"]:
            assert item["role_in_bundle"]

    def test_every_component_is_independently_trust_verified(self, bundle):
        """One unverifiable component contaminates the whole proposal."""
        for item in bundle["items"]:
            assert item["trust_status"] == "PASS"
            assert item["trust_token_ref"].startswith("vc_")

    def test_the_bundle_total_is_the_discounted_subtotal(self, bundle):
        assert bundle["total"] == pytest.approx(
            bundle["subtotal"] - bundle["bundle_discount"], abs=0.01)
        assert bundle["bundle_discount"] >= 0

    def test_the_discount_is_bounded_and_the_bound_is_named(self, bundle):
        assert bundle["guardrails_applied"]
        assert bundle["bundle_discount"] <= bundle["subtotal"] * 0.5

    def test_excluded_items_carry_a_reason(self, bundle):
        for drop in bundle["dropped"]:
            assert drop["reason"]


@requires_gateway
class TestNegotiation:
    @pytest.fixture
    def offer(self, agent):
        resp = call("POST", "/v1/query",
                    {"query": "a waterproof hiking boot, budget is flexible around $200"},
                    token=agent["token"])
        assert resp["outcome"] == "OFFER", resp["result"].get("reason_code")
        return resp["result"]

    def test_an_offer_comes_with_a_negotiation_handle(self, offer):
        assert offer["negotiation_id"]
        assert offer["negotiable"] is True

    def test_a_reachable_counter_is_met(self, agent, offer):
        target = round(offer["price"]["amount"] * 0.96, 2)
        resp = call("POST", "/v1/negotiate", {
            "negotiation_id": offer["negotiation_id"], "target_amount": target},
            token=agent["token"])
        assert resp["outcome"] == "CONCEDED"
        assert resp["amount"] <= target + 0.01

    def test_a_conceded_price_is_a_real_quote_that_can_be_paid(self, agent, offer):
        """A negotiated price that settlement would reject is not a
        negotiation."""
        target = round(offer["price"]["amount"] * 0.95, 2)
        resp = call("POST", "/v1/negotiate", {
            "negotiation_id": offer["negotiation_id"], "target_amount": target},
            token=agent["token"])
        assert resp["quote"], "a concession must issue a quote"
        intent = call("POST", "/v1/principals/intent-mandate", {
            "principal_id": "principal_test", "agent_id": agent["agent_id"],
            "instructions": "buy the negotiated boot", "max_amount": 500.0})
        order = call("POST", "/v1/pay", {
            "principal_id": "principal_test", "intent_mandate": intent,
            "sku": resp["sku"], "quote_id": resp["quote"]["quote_id"],
            "amount": resp["amount"], "trust_token_ref": offer["trust_token_ref"]},
            token=agent["token"])
        assert order["status"] == "SETTLED"

    def test_an_impossible_counter_is_refused_without_leaking_the_floor(self, agent, offer):
        resp = call("POST", "/v1/negotiate", {
            "negotiation_id": offer["negotiation_id"], "target_amount": 1.00},
            token=agent["token"])
        assert resp["outcome"] in {"PARTIAL_CONCESSION", "HELD", "ALTERNATIVE_PROPOSED"}
        blob = str(resp).lower()
        assert "internal_cost" not in blob and "map_price" not in blob
        assert "margin" not in blob

    def test_the_round_limit_is_enforced(self, agent, offer):
        outcomes = []
        for _ in range(5):
            outcomes.append(call("POST", "/v1/negotiate", {
                "negotiation_id": offer["negotiation_id"], "target_amount": 5.00},
                token=agent["token"])["outcome"])
        assert "EXHAUSTED" in outcomes, "an unbounded negotiation is a denial-of-service"

    def test_an_unknown_negotiation_is_a_404(self, agent):
        with pytest.raises(urllib.error.HTTPError) as exc:
            call("POST", "/v1/negotiate",
                 {"negotiation_id": "neg_does_not_exist", "target_amount": 10.0},
                 token=agent["token"])
        assert exc.value.code == 404

    def test_negotiating_without_a_token_is_rejected(self, offer):
        with pytest.raises(urllib.error.HTTPError) as exc:
            call("POST", "/v1/negotiate",
                 {"negotiation_id": offer["negotiation_id"], "target_amount": 10.0})
        assert exc.value.code == 401


@requires_gateway
class TestBundleCheckout:
    def test_a_kit_settles_as_one_signed_cart_with_many_lines(self, agent):
        resp = call("POST", "/v1/query", {
            "query": ("everything a beginner needs for a first overnight trip, he gets cold "
                      "easily, budget around $450")},
            token=agent["token"])
        assert resp["outcome"] == "OFFER"
        offer = resp["result"]
        bundle = offer["bundle"]
        assert bundle and len(bundle["items"]) >= 2

        intent = call("POST", "/v1/principals/intent-mandate", {
            "principal_id": "principal_test", "agent_id": agent["agent_id"],
            "instructions": "buy the beginner kit", "max_amount": 900.0})
        items = [{"sku": i["sku"], "quote_id": i["price"]["quote_id"],
                  "amount": i["price"]["amount"], "trust_token_ref": i["trust_token_ref"]}
                 for i in bundle["items"]]
        order = call("POST", "/v1/pay", {
            "principal_id": "principal_test", "intent_mandate": intent,
            "sku": bundle["items"][0]["sku"],
            "quote_id": bundle["items"][0]["price"]["quote_id"],
            "amount": bundle["total"], "trust_token_ref": bundle["items"][0]["trust_token_ref"],
            "items": items, "bundle_id": bundle["bundle_id"]},
            token=agent["token"])
        assert order["status"] == "SETTLED", order.get("reason_code")

        detail = call("GET", f"/v1/orders/{order['order_id']}")
        cart = detail.get("cart_mandate") or detail["order"]["cart_mandate"]
        if isinstance(cart, str):
            cart = json.loads(cart)
        assert len(cart["items"]) == len(items)
        assert cart["signature"]

    def test_a_cart_claiming_more_than_its_lines_is_refused(self, agent):
        """The arithmetic still works out, which is exactly why it has to be
        checked rather than assumed."""
        resp = call("POST", "/v1/query",
                    {"query": "a water filter for day hikes under $60"}, token=agent["token"])
        offer = resp["result"]
        intent = call("POST", "/v1/principals/intent-mandate", {
            "principal_id": "principal_test", "agent_id": agent["agent_id"],
            "instructions": "buy a filter", "max_amount": 900.0})
        order = call("POST", "/v1/pay", {
            "principal_id": "principal_test", "intent_mandate": intent,
            "sku": offer["sku"], "quote_id": offer["price"]["quote_id"],
            "amount": offer["price"]["amount"] * 3,
            "trust_token_ref": offer["trust_token_ref"],
            "items": [{"sku": offer["sku"], "quote_id": offer["price"]["quote_id"],
                       "amount": offer["price"]["amount"],
                       "trust_token_ref": offer["trust_token_ref"]}]},
            token=agent["token"])
        assert order["status"] == "REJECTED"
        assert order["reason_code"] == "BUNDLE_TOTAL_EXCEEDS_LINES"
