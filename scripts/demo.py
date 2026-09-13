#!/usr/bin/env python3
"""End-to-end demo: a buyer's agent shopping against AgentMarket OS.

Every call below goes over real HTTP through the gateway, exactly as an
external agent would make it. Nothing here reaches into a Python object that
lives in another service.

    python scripts/demo.py                 # all scenarios
    python scripts/demo.py --only 2        # one scenario
    python scripts/demo.py --json          # machine-readable transcript

The scenarios are chosen to show the four things the challenge asks a
merchant-side system to do -- decode a complex intention, prove its claims,
justify its answer, and close the sale through an API -- plus the three
refusals that make the rest of it credible.
"""
from __future__ import annotations

import argparse
import json
import sys
import textwrap
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentmarket_core.config import settings  # noqa: E402

GATEWAY = "http://127.0.0.1:8080"
PRINCIPAL = "principal_luke"

BOLD, DIM, GREEN, RED, YELLOW, CYAN, RESET = (
    "\033[1m", "\033[2m", "\033[32m", "\033[31m", "\033[33m", "\033[36m", "\033[0m")

transcript: list[dict] = []
JSON_MODE = False


def out(*args) -> None:
    if not JSON_MODE:
        print(*args)


def wrap(text: str, indent: str = "    ") -> str:
    return textwrap.fill(str(text), width=96, initial_indent=indent, subsequent_indent=indent)


def call(path: str, payload: dict | None = None, token: str | None = None,
         method: str | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(GATEWAY + path, data=data,
                                 method=method or ("POST" if data else "GET"))
    req.add_header("content-type", "application/json")
    if token:
        req.add_header("authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body = {"_error": exc.code, "_detail": exc.read().decode()[:400]}
    except urllib.error.URLError as exc:
        print(f"{RED}cannot reach the gateway at {GATEWAY}: {exc.reason}{RESET}")
        print("start the stack first:  make up     (or)  ./scripts/run_local.sh start")
        raise SystemExit(1)
    transcript.append({"path": path, "request": payload, "response": body})
    return body


def header(number: int, title: str, why: str) -> None:
    out(f"\n{BOLD}{'=' * 98}{RESET}")
    out(f"{BOLD}  {number}. {title}{RESET}")
    out(f"{DIM}     {why}{RESET}")
    out(f"{BOLD}{'=' * 98}{RESET}")


def show_intent(intent: dict) -> None:
    out(f"\n  {CYAN}decoded intent{RESET} ({intent.get('decoded_by')})")
    out(wrap(intent.get("interpreted_need", ""), "    "))
    hard = [c for c in intent.get("constraints", []) if c["kind"] == "hard"]
    soft = [c for c in intent.get("constraints", []) if c["kind"] == "soft"]
    for label, group in (("must", hard), ("prefer", soft)):
        for c in group:
            phrase = f'  <- "{c["source_phrase"]}"' if c.get("source_phrase") else ""
            out(f"      {label:6} {c['field']} {c['op']} {c['value']}{DIM}{phrase}{RESET}")


def show_offer(result: dict) -> None:
    price = result["price"]
    bundle = result.get("bundle")
    payable = bundle["total"] if bundle else price["amount"]
    out(f"\n  {GREEN}OFFER{RESET}  {BOLD}{result['name']}{RESET}   "
        f"{BOLD}${payable:,.2f} {price['currency']}{RESET}")
    out(f"      trust: {result['trust_status']} ({result['trust_confidence']:.2f})  "
        f"token {result['trust_token_ref'][:18]}")

    if bundle:
        out(f"\n  {CYAN}kit{RESET}  {len(bundle['items'])} items  "
            f"(subtotal ${bundle['subtotal']:,.2f}, bundle adjustment "
            f"-${bundle['bundle_discount']:,.2f}, guardrails {bundle['guardrails_applied']})")
        for item in bundle["items"]:
            out(f"      {item['name'][:38]:38} ${item['price']['amount']:8,.2f}   "
                f"{DIM}{item['role_in_bundle']}{RESET}")
        for drop in bundle["dropped"][:4]:
            out(f"      {DIM}excluded: {drop['name'][:34]:34} {drop['reason']}{RESET}")

    why = result.get("rationale") or {}
    if why:
        out(f"\n  {CYAN}justification{RESET} "
            f"[grounding: {why['grounding']['status']}, composed by {why['grounding']['composed_by']}]")
        out(wrap(why["summary"], "    "))
        if why["matched"]:
            out(f"\n  {CYAN}requirement-by-requirement{RESET}")
            for m in why["matched"][:6]:
                mark = f"{GREEN}PASS{RESET}"
                out(f"      {mark}  {m['requirement'][:48]:48} {DIM}{m['evidence'][:52]}{RESET}")
        for t in why["tradeoffs"][:3]:
            out(f"      {YELLOW}NOTE{RESET}  {t[:100]}")
        verified = [c for c in why["verified_claims"] if c["status"] == "VERIFIED"]
        if verified:
            out(f"\n  {CYAN}values, with evidence{RESET}")
            for c in verified:
                out(f"      {GREEN}VERIFIED{RESET} {c['label']:22} attested by {c['attested_by']}"
                    f" {DIM}({c['certificate']}){RESET}")
        unattested = [c for c in why["verified_claims"] if c["status"] == "ASSERTED_UNATTESTED"]
        for c in unattested:
            out(f"      {RED}UNATTESTED{RESET} {c['label']} -- asserted by the merchant, "
                f"nothing in the chain backs it")
        if why["rejected_alternatives"]:
            out(f"\n  {CYAN}considered and rejected{RESET}")
            for alt in why["rejected_alternatives"]:
                out(f"      {alt['name'][:36]:36} {DIM}{alt['reason']}{RESET}")


def show_rejection(result: dict) -> None:
    out(f"\n  {RED}REFUSED{RESET}  reason_code = {BOLD}{result['reason_code']}{RESET}")
    out(wrap(result["detail"], "    "))
    considered = result.get("considered") or []
    if considered:
        out(f"\n  {CYAN}what was considered{RESET}")
        for c in considered[:4]:
            verdict = f"{GREEN}eligible{RESET}" if c["eligible"] else f"{RED}failed{RESET}"
            out(f"      {verdict}  {c['name'][:34]:34} {DIM}{c['disqualified_by'] or ''}{RESET}")


# ---------------------------------------------------------------------------

def scenario_1_identity() -> str:
    header(1, "Agent identity",
           "Every request is authenticated. An anonymous agent gets a 401, not a price.")
    body = call("/v1/agents/token", {"agent_name": "acme-shopping-agent"})
    out(f"\n  agent_id  {body['agent_id']}")
    out(f"  token     {body['token'][:52]}...  (expires in {body['expires_in']}s)")

    anon = call("/v1/query", {"query": "waterproof boot"})
    out(f"  without a token: HTTP {anon.get('_error')} -- {json.loads(anon['_detail'])['detail']}"
        if anon.get("_error") else "  (unauthenticated call unexpectedly succeeded)")
    return body["token"]


def scenario_2_complex_intent(token: str) -> dict:
    header(2, "A complex, multi-constraint intention",
           "Lifestyle context, an inferred experience level, a values demand and a budget -- "
           "none of it expressible as keywords.")
    query = ("My dad is turning 60 and wants to start hiking. He has never done it before, "
             "he gets cold easily, and I only want brands that can actually prove they're "
             "ethically made. Budget is around $400 for the whole kit.")
    out(f'\n  {BOLD}agent asks:{RESET} "{query}"')
    res = call("/v1/query", {"query": query}, token=token)
    result = res["result"]
    show_intent(result.get("intent") or {})
    show_offer(result)
    out(f"\n  {DIM}trace {res['trace_id']}  |  negotiation {result.get('negotiation_id')}{RESET}")
    return result


def scenario_3_negotiation(token: str, offer: dict) -> dict:
    header(3, "Agent-to-agent negotiation",
           "The buyer's agent counters. The merchant's agent answers with price, then with "
           "structure -- and never reveals a floor.")
    negotiation_id = offer.get("negotiation_id")
    if not negotiation_id:
        out(f"  {YELLOW}no negotiation handle on this offer{RESET}")
        return {}

    bundle = offer.get("bundle")
    standing = bundle["total"] if bundle else offer["price"]["amount"]
    latest: dict = {}
    for target in (round(standing * 0.93, 2), round(standing * 0.72, 2), 40.00):
        out(f"\n  {BOLD}buyer agent:{RESET} \"our ceiling is ${target:,.2f}\"")
        res = call("/v1/negotiate",
                   {"negotiation_id": negotiation_id, "target_amount": target},
                   token=token)
        if res.get("_error"):
            out(f"  {RED}error {res['_error']}{RESET} {res['_detail'][:160]}")
            break
        latest = res
        colour = GREEN if res["outcome"] in {"CONCEDED", "BUNDLE_RESTRUCTURED"} else YELLOW
        out(f"  {BOLD}merchant agent:{RESET} {colour}{res['outcome']}{RESET} at "
            f"{BOLD}${res['amount']:,.2f}{RESET}"
            + (f"  [{res['reason_code']}]" if res["reason_code"] else ""))
        out(wrap(res["message"], "      "))
        if res.get("bundle"):
            for item in res["bundle"]["items"]:
                out(f"        {item['name'][:36]:36} ${item['price']['amount']:8,.2f}")
        out(f"      {DIM}rounds used {res['rounds_used']}, remaining {res['rounds_remaining']}{RESET}")

    out(f"\n  {DIM}note: no response above contains internal cost or MAP. The buyer learns the "
        f"price and the reason, never the margin.{RESET}")
    return latest


def scenario_4_payment(token: str, offer: dict, negotiated: dict) -> dict:
    header(4, "Closing the loop -- AP2 mandates over an API",
           "Intent Mandate, then a Cart Mandate that binds every line to its quote and its "
           "credential. Settlement re-verifies all of it.")

    bundle = (negotiated.get("bundle") if negotiated else None) or offer.get("bundle")
    if bundle:
        items = [{"sku": i["sku"], "quote_id": i["price"]["quote_id"],
                  "amount": i["price"]["amount"], "trust_token_ref": i["trust_token_ref"]}
                 for i in bundle["items"]]
        amount, primary = bundle["total"], bundle["items"][0]
    else:
        items = []
        amount, primary = offer["price"]["amount"], {
            "sku": offer["sku"], "price": offer["price"],
            "trust_token_ref": offer["trust_token_ref"]}

    intent = call("/v1/principals/intent-mandate", {
        "principal_id": PRINCIPAL, "agent_id": "acme-shopping-agent",
        "instructions": "Buy a beginner hiking kit from verifiably ethical brands",
        "max_amount": 500.00,
    })
    out(f"\n  {CYAN}Intent Mandate{RESET}  {intent['mandate_id']}  cap ${intent['max_amount']:,.2f}")
    out(f"      signature {intent['signature'][:56]}...")

    order = call("/v1/pay", {
        "principal_id": PRINCIPAL, "intent_mandate": intent,
        "sku": primary["sku"], "quote_id": primary["price"]["quote_id"],
        "amount": amount, "trust_token_ref": primary["trust_token_ref"],
        "items": items, "bundle_id": bundle["bundle_id"] if bundle else None,
    }, token=token)

    if order.get("_error"):
        out(f"  {RED}payment error {order['_error']}{RESET} {order['_detail'][:200]}")
        return {}

    colour = GREEN if order["status"] == "SETTLED" else RED
    out(f"\n  {colour}{order['status']}{RESET}  ${order['amount']:,.2f} via {order['rail']}"
        f"  ref {order['settlement_ref']}"
        + (f"  [{order['reason_code']}]" if order.get("reason_code") else ""))

    detail = call(f"/v1/orders/{order['order_id']}")
    cart = (detail.get("order") or {}).get("cart_mandate") or detail.get("cart_mandate")
    if isinstance(cart, str):
        cart = json.loads(cart)
    if cart:
        out(f"\n  {CYAN}Cart Mandate{RESET}  {cart['mandate_id']}  -> intent {cart['intent_mandate_id']}")
        for line in cart.get("items") or []:
            out(f"      {line['sku']}  quote {line['quote_id']}  ${line['amount']:,.2f}  "
                f"token {line['trust_token_ref'][:16]}")
        out(f"      signature {cart['signature'][:56]}...")
        out(f"\n  {DIM}Each line was re-validated at settlement: quote still live, credential "
            f"still valid. The cart the principal signed is the cart that was charged.{RESET}")
    return order


def scenario_5_greenwashing(token: str) -> None:
    header(5, "Values that are proven, not asserted",
           "Two products claim recycled materials. Only one has an auditor's certification "
           "event in its provenance chain.")
    query = ("I need a waterproof rain jacket for day hikes, but only from a brand that can "
             "prove its recycled-material claim. Under $220.")
    out(f'\n  {BOLD}agent asks:{RESET} "{query}"')
    res = call("/v1/query", {"query": query}, token=token)
    result = res["result"]
    if res["outcome"] == "OFFER":
        show_offer(result)
    else:
        show_rejection(result)

    out(f"\n  {CYAN}the same question asked directly of the verification API{RESET}")
    for sku, label in (("0950600013459", "SummitShell (attested)"),
                       ("0950600013534", "EcoTrail (asserted only)")):
        body = call(f"/v1/claims/{sku}")
        for claim in body.get("claims", []):
            if claim["claim"] != "recycled_materials":
                continue
            mark = GREEN if claim["status"] == "VERIFIED" else RED
            out(f"      {label:26} recycled_materials -> {mark}{claim['status']}{RESET}"
                + (f"  by {claim['attested_by']}" if claim["attested_by"] else ""))


def scenario_6_refusals(token: str) -> None:
    header(6, "Refusals that are useful",
           "A refusal names the binding constraint, so the buyer's agent can relax it and "
           "re-ask instead of guessing.")
    for query in ("I need a sleeping bag rated to -12C for alpine conditions",
                  "titanium spaceship engine under $5"):
        out(f'\n  {BOLD}agent asks:{RESET} "{query}"')
        res = call("/v1/query", {"query": query}, token=token)
        if res["outcome"] == "OFFER":
            out(f"  {YELLOW}unexpectedly offered {res['result']['name']}{RESET}")
        else:
            show_rejection(res["result"])


def scenario_7_recall(token: str) -> None:
    header(7, "A recall mid-transaction",
           "A credential is revoked between the offer and the payment. Settlement re-verifies, "
           "so the sale stops.")
    query = "waterproof hiking boot under $170"
    res = call("/v1/query", {"query": query}, token=token)
    if res["outcome"] != "OFFER":
        out(f"  {YELLOW}no offer to revoke against ({res['result'].get('reason_code')}){RESET}")
        return
    offer = res["result"]
    out(f"\n  offer issued: {offer['name']} at ${offer['price']['amount']:,.2f}, "
        f"token {offer['trust_token_ref'][:18]}")

    revoked = call("/v1/ops/revoke-token", {
        "token_id": offer["trust_token_ref"],
        "reason": "SAFETY_RECALL: sole delamination reported in batch",
    }, token=token)
    if revoked.get("_error"):
        out(f"  {YELLOW}revocation endpoint unavailable ({revoked['_error']}); "
            f"skipping{RESET}")
        return
    out(f"  {RED}credential revoked{RESET}  {offer['trust_token_ref'][:18]}  "
        f"(safety recall published to the bus)")

    intent = call("/v1/principals/intent-mandate", {
        "principal_id": PRINCIPAL, "agent_id": "acme-shopping-agent",
        "instructions": "Buy the hiking boot", "max_amount": 300.00,
    })
    order = call("/v1/pay", {
        "principal_id": PRINCIPAL, "intent_mandate": intent, "sku": offer["sku"],
        "quote_id": offer["price"]["quote_id"], "amount": offer["price"]["amount"],
        "trust_token_ref": offer["trust_token_ref"],
    }, token=token)
    if order.get("_error"):
        out(f"  {RED}payment error{RESET} {order['_detail'][:160]}")
        return
    colour = GREEN if order["status"] == "REJECTED" else RED
    out(f"\n  payment attempted with the quote issued moments ago -> "
        f"{colour}{order['status']}{RESET}  [{order.get('reason_code')}]")
    out(f"  {DIM}The offer was valid when it was made. Re-verifying at settlement is what "
        f"makes the recall effective rather than merely recorded.{RESET}")


def scenario_8_ledger() -> None:
    header(8, "The audit trail",
           "Every issuance and revocation is a link in a hash chain, and the chain is checkable.")
    integrity = call("/v1/ops/ledger")
    entries = integrity.get("entries", [])[:6]
    for entry in entries:
        out(f"      seq {entry['seq']:>3}  {entry['event_type']:<7} {entry['token_id'][:18]:20}"
            f" {DIM}{entry['entry_hash'][:16]}... <- {entry['prev_hash'][:16]}...{RESET}")
    check = call("/v1/ops/ledger-integrity")
    if not check.get("_error"):
        mark = GREEN if check.get("intact") else RED
        out(f"\n      chain integrity: {mark}{'INTACT' if check.get('intact') else 'BROKEN'}{RESET}"
            f"  ({check.get('entries', len(entries))} entries verified)")


def main() -> None:
    global JSON_MODE
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", type=int, help="run a single scenario (1-8)")
    parser.add_argument("--json", action="store_true", help="print the HTTP transcript as JSON")
    args = parser.parse_args()
    JSON_MODE = args.json

    out(f"{BOLD}AgentMarket OS -- buyer-agent demo{RESET}")
    out(f"{DIM}gateway {GATEWAY}   llm {'ollama:' + settings.ollama_model if settings.llm_enabled else 'disabled (deterministic decoder)'}{RESET}")

    token = scenario_1_identity()
    offer: dict = {}
    negotiated: dict = {}

    # Scenarios 3 and 4 act on the offer scenario 2 produced, so asking for
    # either of them alone still runs 2 first rather than failing.
    if args.only in (None, 2, 3, 4):
        offer = scenario_2_complex_intent(token)
    if args.only in (None, 3, 4) and offer:
        negotiated = scenario_3_negotiation(token, offer)
    if args.only in (None, 4) and offer:
        scenario_4_payment(token, offer, negotiated)
    if args.only in (None, 5):
        scenario_5_greenwashing(token)
    if args.only in (None, 6):
        scenario_6_refusals(token)
    if args.only in (None, 7):
        scenario_7_recall(token)
    if args.only in (None, 8):
        scenario_8_ledger()

    if JSON_MODE:
        print(json.dumps(transcript, indent=2))
    else:
        out(f"\n{BOLD}{'=' * 98}{RESET}")
        out(f"{BOLD}  done.{RESET} {DIM}Ops dashboard: http://localhost:3000/ops{RESET}")


if __name__ == "__main__":
    main()
