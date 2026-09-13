// Single typed client for the AgentMarket OS gateway. Every fetch call in
// the app goes through the functions below, so the base URL and the
// Authorization header both live in exactly one place.

export const GATEWAY_URL =
  process.env.NEXT_PUBLIC_GATEWAY_URL?.replace(/\/$/, "") ||
  "http://localhost:8080";

export class ApiError extends Error {
  status?: number;
  constructor(message: string, status?: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function request<T>(
  path: string,
  init?: RequestInit,
  token?: string,
): Promise<T> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(init?.headers as Record<string, string> | undefined),
  };
  if (token) headers["Authorization"] = `Bearer ${token}`;

  let res: Response;
  try {
    res = await fetch(`${GATEWAY_URL}${path}`, { ...init, headers });
  } catch {
    throw new ApiError(
      `Could not reach the gateway at ${GATEWAY_URL}. Is it running?`,
    );
  }

  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      if (typeof body?.detail === "string") detail = body.detail;
      else if (body?.detail) detail = JSON.stringify(body.detail);
    } catch {
      // ignore -- non-JSON error body
    }
    throw new ApiError(`${path} failed (${res.status}): ${detail}`, res.status);
  }

  return (await res.json()) as T;
}

// ---------------------------------------------------------------------------
// Identity
// ---------------------------------------------------------------------------

export interface TokenResponse {
  agent_id: string;
  token: string;
  expires_in: number;
}

export function getAgentToken(agentName: string): Promise<TokenResponse> {
  return request<TokenResponse>("/v1/agents/token", {
    method: "POST",
    body: JSON.stringify({ agent_name: agentName }),
  });
}

// ---------------------------------------------------------------------------
// Query / storefront
// ---------------------------------------------------------------------------

export interface PriceQuote {
  quote_id: string;
  sku: string;
  amount: number;
  currency: string;
  spread: number;
  fair_value: number;
  issued_at: number;
  valid_until: number;
  guardrails_applied: string[];
}

export interface OfferResult {
  schema_version: string;
  offer_id: string;
  sku: string;
  name: string;
  attributes: Record<string, unknown>;
  price: PriceQuote;
  trust_token_ref: string;
  trust_status: string;
  trust_confidence: number;
}

export interface RejectionResult {
  schema_version: string;
  sku: string | null;
  query: string;
  reason_code: string;
  detail: string;
}

export type QueryOutcome = "OFFER" | "REJECTED";

export interface QueryResponse {
  trace_id: string;
  outcome: QueryOutcome;
  result: OfferResult | RejectionResult;
}

export function isOfferResult(
  r: OfferResult | RejectionResult,
  outcome: QueryOutcome,
): r is OfferResult {
  return outcome === "OFFER";
}

export function runQuery(token: string, query: string): Promise<QueryResponse> {
  return request<QueryResponse>(
    "/v1/query",
    { method: "POST", body: JSON.stringify({ query }) },
    token,
  );
}

// ---------------------------------------------------------------------------
// Tracing
// ---------------------------------------------------------------------------

export interface TraceSpan {
  span_id: string;
  trace_id: string;
  name: string;
  service: string;
  started_at: number;
  ended_at: number;
  duration_ms: number;
  status: string;
  attributes: Record<string, unknown>;
}

export interface TraceResponse {
  trace_id: string;
  spans: TraceSpan[];
}

export function getTrace(traceId: string): Promise<TraceResponse> {
  return request<TraceResponse>(`/v1/trace/${encodeURIComponent(traceId)}`);
}

// ---------------------------------------------------------------------------
// Payments
// ---------------------------------------------------------------------------

export interface IntentMandate {
  mandate_id: string;
  agent_id: string;
  principal_id: string;
  instructions: string;
  max_amount: number;
  currency: string;
  issued_at: number;
  signature: string;
}

export function createIntentMandate(params: {
  principal_id: string;
  agent_id: string;
  instructions: string;
  max_amount: number;
}): Promise<IntentMandate> {
  return request<IntentMandate>("/v1/principals/intent-mandate", {
    method: "POST",
    body: JSON.stringify(params),
  });
}

export interface OrderResult {
  order_id: string;
  sku: string;
  amount: number;
  currency: string;
  rail: "card_network" | "stablecoin_x402" | null;
  settlement_ref: string;
  status: "SETTLED" | "REJECTED";
  reason_code: string | null;
  settled_at: number;
}

export function pay(
  token: string,
  params: {
    principal_id: string;
    intent_mandate: IntentMandate;
    sku: string;
    quote_id: string;
    amount: number;
    trust_token_ref: string;
  },
): Promise<OrderResult> {
  return request<OrderResult>(
    "/v1/pay",
    { method: "POST", body: JSON.stringify(params) },
    token,
  );
}

/** The Cart Mandate the gateway composed and signed on the agent's behalf. */
export interface CartMandate {
  mandate_id: string;
  intent_mandate_id: string;
  agent_id: string;
  sku: string;
  quote_id: string;
  amount: number;
  currency: string;
  trust_token_ref: string;
  signed_at: number;
  signature: string;
}

export interface OrderDetail {
  order_id: string;
  status: "SETTLED" | "REJECTED";
  cart_mandate: CartMandate | null;
  intent_mandate: IntentMandate | null;
}

/**
 * Fetch the stored order, including the signed mandate chain. The gateway
 * composes the cart mandate internally during /v1/pay and returns only the
 * settlement result, so this is how the console shows what was actually
 * authorized.
 */
export function getOrder(orderId: string): Promise<OrderDetail> {
  return request<OrderDetail>(`/v1/orders/${encodeURIComponent(orderId)}`);
}

// ---------------------------------------------------------------------------
// Ops overview
// ---------------------------------------------------------------------------

export interface MarketSkuSummary {
  sku: string;
  name: string;
  list_price: number;
  map_price: number;
  inventory_units: number;
  competitor_count: number;
  competitor_min: number;
  competitor_max: number;
}

export interface ProvenanceSkuSummary {
  sku: string;
  name: string;
  batch: string;
  ready: boolean;
  missing_steps: string[];
  present_steps: string[];
  event_chain_hash: string;
  reason_code: string | null;
}

export interface LedgerStats {
  entries: number;
  issued: number;
  revoked: number;
}

export interface LedgerSummary {
  intact: boolean;
  error: string | null;
  stats: LedgerStats;
}

export interface OrderSummary {
  order_id: string;
  sku: string;
  amount: number;
  currency: string;
  rail: string | null;
  settlement_ref: string;
  status: string;
  reason_code: string | null;
  agent_id: string;
  created_at: string;
}

export interface TraceSummary {
  trace_id: string;
  started_at: number;
  total_ms: number;
  spans: number;
  had_error: boolean;
  outcome: string | null;
  query: string;
}

// `backends` is a heterogeneous bag: most values are simple status blocks,
// but some (e.g. storefront's `pricing`/`verification` entries) are full
// nested ServiceStatus objects. We only ever render it as formatted JSON /
// keyed rows, so an index signature is the honest type.
export interface ServiceStatus {
  service: string;
  status: "ok" | "degraded" | string;
  version: string;
  environment: string;
  backends: Record<string, unknown>;
  detail: string | null;
}

export interface OpsOverview {
  market: { skus: MarketSkuSummary[] };
  provenance: { skus: ProvenanceSkuSummary[] };
  ledger: LedgerSummary;
  orders: { orders: OrderSummary[] };
  traces: { traces: TraceSummary[] };
  services: Record<string, ServiceStatus>;
}

export function getOpsOverview(): Promise<OpsOverview> {
  return request<OpsOverview>("/v1/ops/overview");
}

export interface CompetitorObservation {
  competitor: string;
  price: number;
  observed_at: number;
}

export interface BanditArm {
  spread: number;
  pulls: number;
  mean_reward: number;
}

export interface MarketDetail {
  sku: string;
  name: string;
  competitors: CompetitorObservation[];
  competitor_prices: number[];
  map_price: number;
  list_price: number;
  inventory_units: number;
  fair_value: number;
  margin_floor: number;
  bandit_arms: BanditArm[];
}

export function getMarketDetail(sku: string): Promise<MarketDetail> {
  return request<MarketDetail>(`/v1/ops/market/${encodeURIComponent(sku)}`);
}

export interface LedgerEntry {
  seq: number;
  event_type: string;
  token_id: string;
  prev_hash: string;
  entry_hash: string;
  created_at: string;
}

export interface LedgerDetail {
  entries: LedgerEntry[];
  stats: LedgerStats;
}

export function getLedger(limit = 50): Promise<LedgerDetail> {
  return request<LedgerDetail>(`/v1/ops/ledger?limit=${limit}`);
}

export interface EventEnvelope {
  event_id: string;
  topic: string;
  ts: number;
  source: string;
  payload: Record<string, unknown>;
}

export interface EventsResponse {
  events: EventEnvelope[];
}

export function getEvents(limit = 50): Promise<EventsResponse> {
  return request<EventsResponse>(`/v1/events?limit=${limit}`);
}

export function eventStreamUrl(): string {
  return `${GATEWAY_URL}/v1/events/stream`;
}
