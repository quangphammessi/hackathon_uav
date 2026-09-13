#!/usr/bin/env python3
"""End-to-end CLI walkthrough against a running AgentMarket OS stack.

Drives the system exactly as an autonomous buyer agent would -- over HTTP,
through the gateway, with a bearer token -- rather than importing the
internals. If this passes, the deployed system works.

    ./scripts/run_local.sh start && python scripts/demo.py
    # or, against Compose:
    GATEWAY_URL=http://localhost:8080 python scripts/demo.py

Walks: identity -> three queries covering every branch of the LangGraph
workflow -> AP2 mandate signing and settlement -> a safety recall that blocks
an in-flight payment -> ledger integrity.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

GATEWAY = os.getenv("GATEWAY_URL", "http://127.0.0.1:8080")
VERIFICATION = os.getenv("VERIFICATION_URL", "http://127.0.0.1:8083")

BOOT_QUERY = "I need a waterproof hiking boot under $160"


def call(method: str, path: str, body=None, token: str | None = None, base: str = GATEWAY):
    req = urllib.request.Request(f"{base}{path}", method=method)
    req.add_header("content-type", "application/json")
    if token:
        req.add_header("authorization", f"Bearer {token}")
    data = json.dumps(body).encode() if body is not None else None
    with urllib.request.urlopen(req, data, timeout=60) as resp:
        return json.loads(resp.read())


def header(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def main() -> int:
    try:
        call("GET", "/live")
    except (urllib.error.URLError, OSError) as exc:
        print(f"Cannot reach the gateway at {GATEWAY}: {exc}\n"
              f"Start the stack first:\n"
              f"  ./scripts/run_local.sh start\n"
              f"  docker compose -f infra/docker-compose.yml up --build", file=sys.stderr)
        return 1

    header("AgentMarket OS -- end-to-end demo")
    health = call("GET", "/health")
    print(f"gateway: {health['status']} (environment={health['environment']})")
    for name, svc in call("GET", "/v1/ops/overview")["services"].items():
        detail = f" -- {svc.get('detail')}" if svc.get("detail") else ""
        print(f"  {name:13} {svc.get('status')}{detail}")

    header("1. Agent identity (proposal S8.1)")
    agent = call("POST", "/v1/agents/token", {"agent_name": "DemoShoppingAgent"})
    token, agent_id = agent["token"], agent["agent_id"]
    print(f"issued {agent_id}, expires in {agent['expires_in']}s")

    header("2. Agentic RAG storefront (proposal S5) -- three queries")
    offer = None
    for label, query in [
        ("resolves to a verified offer", BOOT_QUERY),
        ("provenance gap -> refused", "I want the -12C down sleeping bag"),
        ("unsatisfiable -> refused after bounded retries", "a titanium spaceship engine under $5"),
    ]:
        resp = call("POST", "/v1/query", {"query": query}, token=token)
        result = resp["result"]
        print(f"\n> {query!r}   [{label}]")
        print(f"  outcome={resp['outcome']}  trace={resp['trace_id']}")
        if resp["outcome"] == "OFFER":
            print(f"  {result['name']}  {result['price']['currency']} {result['price']['amount']}"
                  f"  (fair value {result['price']['fair_value']}, spread {result['price']['spread']})")
            print(f"  guardrails: {result['price']['guardrails_applied'] or 'none'}")
            print(f"  trust: {result['trust_status']} @ {result['trust_confidence']}"
                  f"  token={result['trust_token_ref']}")
            offer = offer or result
        else:
            print(f"  reason_code={result['reason_code']}")
        spans = call("GET", f"/v1/trace/{resp['trace_id']}")["spans"]
        print("  path: " + " -> ".join(s["name"] for s in spans))

    if not offer:
        print("\nNo offer produced; skipping the payment walkthrough.", file=sys.stderr)
        return 1

    header("3. Payments (proposal S6.4) -- AP2 mandate chain")
    intent = call("POST", "/v1/principals/intent-mandate", {
        "principal_id": "principal_demo", "agent_id": agent_id,
        "instructions": BOOT_QUERY, "max_amount": 500.00,
    })
    print(f"Intent Mandate {intent['mandate_id']} signed (cap {intent['currency']} {intent['max_amount']})")

    order = call("POST", "/v1/pay", {
        "principal_id": "principal_demo", "intent_mandate": intent,
        "sku": offer["sku"], "quote_id": offer["price"]["quote_id"],
        "amount": offer["price"]["amount"], "trust_token_ref": offer["trust_token_ref"],
    }, token=token)
    detail = call("GET", f"/v1/orders/{order['order_id']}")
    cart = detail.get("cart_mandate") or {}
    print(f"Cart Mandate   {cart.get('mandate_id')} binds sku={cart.get('sku')} "
          f"quote={cart.get('quote_id')} amount={cart.get('amount')}")
    print(f"Settlement     {order['status']} via {order['rail']} ref={order['settlement_ref']}")

    header("4. Safety recall -- revocation blocks an in-flight payment")
    resp = call("POST", "/v1/query", {"query": BOOT_QUERY}, token=token)
    if resp["outcome"] == "OFFER":
        fresh = resp["result"]
        print(f"fresh offer {fresh['price']['amount']} with token {fresh['trust_token_ref']}")
        call("POST", "/v1/trust-tokens/revoke",
             {"token_id": fresh["trust_token_ref"], "reason": "safety recall"}, base=VERIFICATION)
        print("credential revoked between offer and payment")
        blocked = call("POST", "/v1/pay", {
            "principal_id": "principal_demo", "intent_mandate": intent,
            "sku": fresh["sku"], "quote_id": fresh["price"]["quote_id"],
            "amount": fresh["price"]["amount"], "trust_token_ref": fresh["trust_token_ref"],
        }, token=token)
        print(f"payment -> {blocked['status']} ({blocked['reason_code']})")

    header("5. Trust Token Ledger integrity (proposal S6.2)")
    ledger = call("GET", "/v1/ops/overview")["ledger"]
    print(f"hash chain intact: {ledger.get('intact')}  stats={ledger.get('stats')}")

    print("\nDone. Ops Dashboard: http://localhost:3000/ops\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
