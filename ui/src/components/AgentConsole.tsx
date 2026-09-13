"use client";

import { useCallback, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import {
  ApiError,
  createIntentMandate,
  getAgentToken,
  getOrder,
  getTrace,
  isOfferResult,
  pay,
  runQuery,
  type CartMandate,
  type IntentMandate,
  type OfferResult,
  type OrderResult,
  type QueryResponse,
  type RejectionResult,
  type TokenResponse,
  type TraceResponse,
  type TraceSpan,
} from "@/lib/api";
import { GraphView } from "@/components/GraphView";
import { SpanDetailPanel } from "@/components/SpanDetailPanel";
import { OfferCard } from "@/components/OfferCard";
import { RejectionCard } from "@/components/RejectionCard";
import { MandateFlow } from "@/components/MandateFlow";
import { StatusBadge } from "@/components/StatusBadge";

const PRESETS: { label: string; query: string; hint: string }[] = [
  {
    label: "Waterproof hiking boot",
    query: "I need a waterproof hiking boot under $160",
    hint: "OFFER",
  },
  {
    label: "-12C sleeping bag",
    query: "I want the -12C down sleeping bag",
    hint: "REJECTED · CHAIN_GAP",
  },
  {
    label: "Titanium spaceship engine",
    query: "a titanium spaceship engine under $5",
    hint: "REJECTED · NO_CANDIDATES",
  },
  {
    label: "Lightweight water filter",
    query: "a lightweight water filter for hiking",
    hint: "OFFER",
  },
];

const PRINCIPAL_ID = "principal_demo";

export function AgentConsole() {
  const searchParams = useSearchParams();
  const initialTraceId = searchParams.get("trace");

  const [token, setToken] = useState<TokenResponse | null>(null);
  const [tokenError, setTokenError] = useState<string | null>(null);
  const [tokenLoading, setTokenLoading] = useState(true);

  const [queryText, setQueryText] = useState("");
  const [submittedQuery, setSubmittedQuery] = useState<string | null>(null);
  const [queryResult, setQueryResult] = useState<QueryResponse | null>(null);
  const [queryLoading, setQueryLoading] = useState(false);
  const [queryError, setQueryError] = useState<string | null>(null);

  const [trace, setTrace] = useState<TraceResponse | null>(null);
  const [traceLoading, setTraceLoading] = useState(false);
  const [archivedTraceId, setArchivedTraceId] = useState<string | null>(null);

  const [selectedNode, setSelectedNode] = useState<
    { id: string; spans: TraceSpan[] } | null
  >(null);

  const [intentMandate, setIntentMandate] = useState<IntentMandate | null>(null);
  const [order, setOrder] = useState<OrderResult | null>(null);
  const [cartMandate, setCartMandate] = useState<CartMandate | null>(null);
  const [payLoading, setPayLoading] = useState(false);
  const [payError, setPayError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setTokenLoading(true);
    getAgentToken("DemoShoppingAgent")
      .then((t) => {
        if (!cancelled) setToken(t);
      })
      .catch((e: unknown) => {
        if (!cancelled) setTokenError(errorMessage(e));
      })
      .finally(() => {
        if (!cancelled) setTokenLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!initialTraceId) return;
    let cancelled = false;
    setTraceLoading(true);
    getTrace(initialTraceId)
      .then((t) => {
        if (cancelled) return;
        setTrace(t);
        setArchivedTraceId(initialTraceId);
      })
      .catch((e: unknown) => {
        if (!cancelled) setQueryError(errorMessage(e));
      })
      .finally(() => {
        if (!cancelled) setTraceLoading(false);
      });
    return () => {
      cancelled = true;
    };
    // Only ever runs once for the initial ?trace= param.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const submit = useCallback(
    async (q: string) => {
      const trimmed = q.trim();
      if (!trimmed || !token) return;
      setArchivedTraceId(null);
      setQueryLoading(true);
      setQueryError(null);
      setQueryResult(null);
      setTrace(null);
      setSelectedNode(null);
      setIntentMandate(null);
      setOrder(null);
      setCartMandate(null);
      setPayError(null);
      setSubmittedQuery(trimmed);
      try {
        const resp = await runQuery(token.token, trimmed);
        setQueryResult(resp);
        setTraceLoading(true);
        try {
          const t = await getTrace(resp.trace_id);
          setTrace(t);
        } finally {
          setTraceLoading(false);
        }
      } catch (e) {
        setQueryError(errorMessage(e));
      } finally {
        setQueryLoading(false);
      }
    },
    [token],
  );

  const handlePay = useCallback(async () => {
    if (!token || !queryResult || queryResult.outcome !== "OFFER") return;
    const offer = queryResult.result as OfferResult;
    setPayLoading(true);
    setPayError(null);
    setCartMandate(null);
    try {
      const intent = await createIntentMandate({
        principal_id: PRINCIPAL_ID,
        agent_id: token.agent_id,
        instructions: submittedQuery ?? offer.name,
        max_amount: 500,
      });
      setIntentMandate(intent);
      const orderResp = await pay(token.token, {
        principal_id: PRINCIPAL_ID,
        intent_mandate: intent,
        sku: offer.sku,
        quote_id: offer.price.quote_id,
        amount: offer.price.amount,
        trust_token_ref: offer.trust_token_ref,
      });
      setOrder(orderResp);
      // The gateway signs the cart mandate itself and returns only the
      // settlement result, so read the stored order back to show the chain.
      try {
        const detail = await getOrder(orderResp.order_id);
        setCartMandate(detail.cart_mandate);
      } catch {
        // Non-fatal: the payment already succeeded or failed on its own
        // terms; not being able to display the cart mandate is cosmetic.
      }
    } catch (e) {
      setPayError(errorMessage(e));
    } finally {
      setPayLoading(false);
    }
  }, [token, queryResult, submittedQuery]);

  const offer =
    queryResult && isOfferResult(queryResult.result, queryResult.outcome)
      ? (queryResult.result as OfferResult)
      : null;
  const rejection =
    queryResult && queryResult.outcome === "REJECTED"
      ? (queryResult.result as RejectionResult)
      : null;

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">Agent Demo Console</h1>
          <p className="mt-1 max-w-2xl text-sm text-text-dim">
            Simulates an autonomous buyer agent querying AgentMarket OS, and
            replays the LangGraph workflow that produced the result.
          </p>
        </div>
        <AgentIdentityBadge
          loading={tokenLoading}
          error={tokenError}
          token={token}
        />
      </div>

      {archivedTraceId && (
        <div className="rounded-lg border border-accent/30 bg-accent/5 px-4 py-2.5 text-sm text-text-dim">
          Viewing archived trace <span className="font-mono text-accent">{archivedTraceId}</span>{" "}
          from the Ops Dashboard. Run a query below to start a new one.
        </div>
      )}

      <section className="rounded-lg border border-border bg-bg-panel p-5">
        <label htmlFor="query" className="text-sm font-medium text-text-dim">
          Agent query
        </label>
        <div className="mt-2 flex flex-col gap-2 sm:flex-row">
          <input
            id="query"
            type="text"
            value={queryText}
            onChange={(e) => setQueryText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") submit(queryText);
            }}
            placeholder="Describe what the agent is shopping for..."
            className="flex-1 rounded-md border border-border bg-bg px-3 py-2 text-sm outline-none placeholder:text-text-faint focus:border-accent"
          />
          <button
            type="button"
            onClick={() => submit(queryText)}
            disabled={!token || queryLoading || !queryText.trim()}
            className="rounded-md border border-accent/40 bg-accent/10 px-4 py-2 text-sm font-medium text-accent transition-colors hover:bg-accent/20 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {queryLoading ? "Running…" : "Run query"}
          </button>
        </div>

        <div className="mt-4 flex flex-wrap gap-2">
          {PRESETS.map((p) => (
            <button
              key={p.query}
              type="button"
              onClick={() => {
                setQueryText(p.query);
                submit(p.query);
              }}
              disabled={!token || queryLoading}
              className="group flex flex-col items-start rounded-md border border-border bg-bg px-3 py-2 text-left text-xs transition-colors hover:border-accent/40 hover:bg-bg-raised disabled:cursor-not-allowed disabled:opacity-40"
            >
              <span className="font-medium text-text">{p.label}</span>
              <span className="mt-0.5 font-mono text-[10px] text-text-faint group-hover:text-text-dim">
                {p.hint}
              </span>
            </button>
          ))}
        </div>

        {tokenError && (
          <p className="mt-3 text-xs text-err">
            Could not obtain an agent token: {tokenError}
          </p>
        )}
      </section>

      {queryError && (
        <div className="rounded-lg border border-err/30 bg-err/5 px-4 py-3 text-sm text-err">
          {queryError}
        </div>
      )}

      {queryLoading && !queryResult && (
        <div className="rounded-lg border border-border bg-bg-panel p-5 text-sm text-text-dim">
          Waiting on the storefront graph…
        </div>
      )}

      {offer && <OfferCard offer={offer} />}
      {rejection && <RejectionCard rejection={rejection} />}

      {offer && (
        <section className="space-y-4">
          {!order && (
            <button
              type="button"
              onClick={handlePay}
              disabled={payLoading}
              className="rounded-md border border-ok/40 bg-ok/10 px-4 py-2 text-sm font-medium text-ok transition-colors hover:bg-ok/20 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {payLoading ? "Signing & settling…" : "Sign mandates & pay"}
            </button>
          )}
          {payError && (
            <div className="rounded-lg border border-err/30 bg-err/5 px-4 py-3 text-sm text-err">
              {payError}
            </div>
          )}
          {(intentMandate || order) && (
            <MandateFlow
              intentMandate={intentMandate}
              cartMandate={cartMandate}
              order={order}
            />
          )}
        </section>
      )}

      <section className="space-y-3">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold text-text-dim">
            LangGraph execution
          </h2>
          {traceLoading && (
            <span className="text-xs text-text-faint">loading trace…</span>
          )}
        </div>
        <GraphView
          spans={trace?.spans ?? null}
          onSelectNode={(id, spans) => setSelectedNode({ id, spans })}
        />
        <p className="text-xs text-text-faint">
          Click a lit node to inspect its recorded attributes -- proof the
          pipeline reasons over real data rather than hallucinating a result.
        </p>
        {selectedNode && (
          <SpanDetailPanel
            nodeId={selectedNode.id}
            spans={selectedNode.spans}
            onClose={() => setSelectedNode(null)}
          />
        )}
      </section>
    </div>
  );
}

function AgentIdentityBadge({
  loading,
  error,
  token,
}: {
  loading: boolean;
  error: string | null;
  token: TokenResponse | null;
}) {
  if (loading) return <StatusBadge tone="neutral">connecting…</StatusBadge>;
  if (error) return <StatusBadge tone="err">gateway unreachable</StatusBadge>;
  if (!token) return null;
  return (
    <StatusBadge tone="accent">
      <span className="font-mono">{token.agent_id}</span>
    </StatusBadge>
  );
}

function errorMessage(e: unknown): string {
  if (e instanceof ApiError) return e.message;
  if (e instanceof Error) return e.message;
  return "Unknown error";
}
