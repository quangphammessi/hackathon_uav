"""Typed HTTP clients for inter-service calls (proposal §7.1).

Every cross-service call in the system goes through one of these, and each one
returns a validated pydantic model rather than a dict. That is what keeps the
"deterministic contracts at every boundary" principle true once the monolith
is split: a service that changes its response shape breaks its callers at the
parse step, loudly, instead of producing a subtly wrong Offer three hops
later.

Retries are deliberately narrow. Only connection errors and 5xx are retried,
and only for idempotent reads. A failed `POST /pay` is never retried
automatically -- at-least-once delivery is the right default for an event bus
and the wrong one for taking someone's money.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from agentmarket_core.config import settings
from agentmarket_core.models import (
    CartMandate,
    IntentMandate,
    OrderResult,
    PriceQuote,
    VerificationResult,
)

log = logging.getLogger("agentmarket.clients")

RETRYABLE_STATUS = {502, 503, 504}


class ServiceError(RuntimeError):
    """A sibling service was unreachable or returned an error we cannot use."""

    def __init__(self, service: str, detail: str, status_code: int | None = None) -> None:
        super().__init__(f"{service}: {detail}")
        self.service = service
        self.detail = detail
        self.status_code = status_code


class _BaseClient:
    service = "service"

    def __init__(self, base_url: str, timeout: float | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout or settings.internal_timeout_seconds,
            headers={"user-agent": f"agentmarket/{settings.service_name}"},
        )

    def _request(self, method: str, path: str, *, retries: int = 0, **kwargs: Any) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                resp = self._client.request(method, path, **kwargs)
                if resp.status_code in RETRYABLE_STATUS and attempt < retries:
                    time.sleep(0.15 * (attempt + 1))
                    continue
                if resp.status_code >= 400:
                    raise ServiceError(self.service, resp.text[:300], resp.status_code)
                return resp
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt < retries:
                    time.sleep(0.15 * (attempt + 1))
                    continue
                raise ServiceError(self.service, str(exc)) from exc
        raise ServiceError(self.service, str(last_error))

    def health(self) -> dict:
        try:
            return self._request("GET", "/health", retries=0).json()
        except ServiceError as exc:
            return {"service": self.service, "status": "degraded", "detail": exc.detail}


class PricingClient(_BaseClient):
    service = "pricing"

    def __init__(self, base_url: str | None = None) -> None:
        super().__init__(base_url or settings.pricing_url)

    def quote(self, sku: str) -> PriceQuote:
        return PriceQuote(**self._request("POST", "/v1/quote", json={"sku": sku}, retries=2).json())

    def is_quote_valid(self, quote_id: str, sku: str, amount: float) -> bool:
        resp = self._request(
            "POST", "/v1/quote/validate",
            json={"quote_id": quote_id, "sku": sku, "amount": amount}, retries=2,
        )
        return bool(resp.json().get("valid"))

    def record_outcome(self, quote_id: str, won: bool) -> None:
        self._request("POST", "/v1/quote/outcome", json={"quote_id": quote_id, "won": won})


class VerificationClient(_BaseClient):
    service = "verification"

    def __init__(self, base_url: str | None = None) -> None:
        super().__init__(base_url or settings.verification_url)

    def ensure_token(self, sku: str) -> tuple[str | None, str | None]:
        body = self._request("POST", "/v1/trust-tokens/ensure", json={"sku": sku}, retries=2).json()
        return body.get("token_id"), body.get("reason_code")

    def verify(self, trust_token_ref: str) -> VerificationResult:
        return VerificationResult(
            **self._request("POST", "/v1/verify", json={"trust_token_ref": trust_token_ref}, retries=2).json()
        )


class PaymentsClient(_BaseClient):
    service = "payments"

    def __init__(self, base_url: str | None = None) -> None:
        super().__init__(base_url or settings.payments_url)

    def create_intent_mandate(
        self, principal_id: str, agent_id: str, instructions: str, max_amount: float
    ) -> IntentMandate:
        return IntentMandate(**self._request("POST", "/v1/mandates/intent", json={
            "principal_id": principal_id, "agent_id": agent_id,
            "instructions": instructions, "max_amount": max_amount,
        }).json())

    def create_cart_mandate(
        self, principal_id: str, intent_mandate: IntentMandate, sku: str,
        quote_id: str, amount: float, trust_token_ref: str,
    ) -> CartMandate:
        return CartMandate(**self._request("POST", "/v1/mandates/cart", json={
            "principal_id": principal_id, "intent_mandate": intent_mandate.model_dump(),
            "sku": sku, "quote_id": quote_id, "amount": amount,
            "trust_token_ref": trust_token_ref,
        }).json())

    def pay(self, principal_id: str, intent: IntentMandate, cart: CartMandate) -> OrderResult:
        # No retries: replaying a settlement request is how you double-charge.
        return OrderResult(**self._request("POST", "/v1/pay", json={
            "principal_id": principal_id,
            "intent_mandate": intent.model_dump(),
            "cart_mandate": cart.model_dump(),
        }).json())


class StorefrontClient(_BaseClient):
    service = "storefront"

    def __init__(self, base_url: str | None = None) -> None:
        super().__init__(base_url or settings.storefront_url, timeout=60.0)

    def query(self, agent_id: str, query: str) -> dict:
        return self._request("POST", "/v1/query", json={"agent_id": agent_id, "query": query}).json()
