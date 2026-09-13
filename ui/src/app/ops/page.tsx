"use client";

import { useCallback, useEffect, useState } from "react";
import {
  ApiError,
  getClaims,
  getLedger,
  getMarketDetail,
  getNegotiations,
  getOpsOverview,
  type ClaimVerification,
  type LedgerDetail,
  type MarketDetail,
  type NegotiationSummary,
  type OpsOverview,
} from "@/lib/api";
import { ServiceHealthStrip } from "@/components/ops/ServiceHealthStrip";
import { PricingTable } from "@/components/ops/PricingTable";
import { MarketDetailPanel } from "@/components/ops/MarketDetailPanel";
import { ProvenanceTable } from "@/components/ops/ProvenanceTable";
import { LedgerPanel } from "@/components/ops/LedgerPanel";
import { OrdersTable } from "@/components/ops/OrdersTable";
import { TracesTable } from "@/components/ops/TracesTable";
import { NegotiationsPanel } from "@/components/ops/NegotiationsPanel";
import { ClaimsPanel } from "@/components/ops/ClaimsPanel";
import { EventFeed } from "@/components/ops/EventFeed";

function Section({
  title,
  description,
  children,
}: {
  title: string;
  description?: string;
  children: React.ReactNode;
}) {
  return (
    <section className="space-y-3 rounded-lg border border-border bg-bg-panel p-5">
      <div>
        <h2 className="text-sm font-semibold">{title}</h2>
        {description && (
          <p className="mt-0.5 text-xs text-text-faint">{description}</p>
        )}
      </div>
      {children}
    </section>
  );
}

// EcoTrail Recycled Rain Shell: asserts "recycled materials" and "low-carbon
// transport", and its provenance chain attests neither.
const GREENWASHED_EXAMPLE_SKU = "0950600013534";

export default function OpsPage() {
  const [overview, setOverview] = useState<OpsOverview | null>(null);
  const [overviewError, setOverviewError] = useState<string | null>(null);
  const [overviewLoading, setOverviewLoading] = useState(true);

  const [selectedSku, setSelectedSku] = useState<string | null>(null);
  const [marketDetail, setMarketDetail] = useState<MarketDetail | null>(null);
  const [marketLoading, setMarketLoading] = useState(false);
  const [marketError, setMarketError] = useState<string | null>(null);

  const [ledgerDetail, setLedgerDetail] = useState<LedgerDetail | null>(null);
  const [verifying, setVerifying] = useState(false);

  const [negotiations, setNegotiations] = useState<NegotiationSummary[] | null>(null);
  const [negotiationsError, setNegotiationsError] = useState<string | null>(null);

  const [claimsSku, setClaimsSku] = useState<string | null>(null);
  const [claims, setClaims] = useState<ClaimVerification[] | null>(null);
  const [claimsLoading, setClaimsLoading] = useState(false);
  const [claimsError, setClaimsError] = useState<string | null>(null);

  const loadOverview = useCallback(() => {
    setOverviewLoading(true);
    setOverviewError(null);
    getOpsOverview()
      .then(setOverview)
      .catch((e: unknown) => setOverviewError(errorMessage(e)))
      .finally(() => setOverviewLoading(false));
  }, []);

  useEffect(() => {
    loadOverview();
  }, [loadOverview]);

  useEffect(() => {
    getLedger(50)
      .then(setLedgerDetail)
      .catch(() => {
        // the summary panel already reports ledger health; entries are best-effort
      });
  }, []);

  useEffect(() => {
    getNegotiations(25)
      .then((r) => setNegotiations(r.negotiations))
      .catch((e: unknown) => setNegotiationsError(errorMessage(e)));
  }, []);

  const lookupClaims = useCallback((sku: string) => {
    setClaimsSku(sku);
    setClaimsLoading(true);
    setClaimsError(null);
    getClaims(sku)
      .then((r) => setClaims(r.claims))
      .catch((e: unknown) => setClaimsError(errorMessage(e)))
      .finally(() => setClaimsLoading(false));
  }, []);

  // Open on the product that asserts two values claims and has attestation for
  // neither. An empty panel makes the merchant look compliant by default; the
  // point of this panel is that the gap is visible without being hunted for.
  useEffect(() => {
    lookupClaims(GREENWASHED_EXAMPLE_SKU);
  }, [lookupClaims]);

  const selectSku = useCallback((sku: string) => {
    setSelectedSku(sku);
    setMarketLoading(true);
    setMarketError(null);
    getMarketDetail(sku)
      .then(setMarketDetail)
      .catch((e: unknown) => setMarketError(errorMessage(e)))
      .finally(() => setMarketLoading(false));
  }, []);

  const verifyLedger = useCallback(() => {
    setVerifying(true);
    getLedger(50)
      .then(setLedgerDetail)
      .catch(() => {})
      .finally(() => setVerifying(false));
  }, []);

  if (overviewLoading && !overview) {
    return <p className="text-sm text-text-dim">Loading ops overview…</p>;
  }

  if (overviewError && !overview) {
    return (
      <div className="rounded-lg border border-err/30 bg-err/5 p-5 text-sm text-err">
        <p className="font-medium">Could not load the ops overview.</p>
        <p className="mt-1 text-xs">{overviewError}</p>
        <button
          type="button"
          onClick={loadOverview}
          className="mt-3 rounded-md border border-err/40 px-3 py-1.5 text-xs font-medium hover:bg-err/10"
        >
          Retry
        </button>
      </div>
    );
  }

  if (!overview) return null;

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">Ops Dashboard</h1>
          <p className="mt-1 text-sm text-text-dim">
            Live state of every AgentMarket OS subsystem.
          </p>
        </div>
        <button
          type="button"
          onClick={loadOverview}
          className="rounded-md border border-border px-3 py-1.5 text-xs text-text-dim hover:bg-bg-raised hover:text-text"
        >
          Refresh
        </button>
      </div>

      {overviewError && (
        <p className="text-xs text-warn">
          Last refresh failed ({overviewError}); showing previously loaded data.
        </p>
      )}

      <Section title="Service health">
        <ServiceHealthStrip services={overview.services} />
      </Section>

      <Section
        title="Pricing"
        description="Competitor-aware pricing that never sells below the margin floor."
      >
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          <PricingTable
            skus={overview.market.skus}
            selectedSku={selectedSku}
            onSelect={selectSku}
          />
          <MarketDetailPanel
            detail={marketDetail}
            loading={marketLoading}
            error={marketError}
          />
        </div>
      </Section>

      <Section
        title="Provenance & trust"
        description="Deterministic verification over supply-chain event chains. Click a row to look up its values claims below."
      >
        <ProvenanceTable
          skus={overview.provenance.skus}
          selectedSku={claimsSku}
          onSelect={lookupClaims}
        />
      </Section>

      <Section
        title="Values claims"
        description="What a product asserts vs. what its provenance actually attests -- the greenwashing detector."
      >
        <ClaimsPanel
          sku={claimsSku}
          claims={claims}
          loading={claimsLoading}
          error={claimsError}
          onLookup={lookupClaims}
        />
      </Section>

      <Section
        title="Ledger integrity"
        description="Hash-chained, append-only trust token ledger."
      >
        <LedgerPanel
          summary={overview.ledger}
          detail={ledgerDetail}
          onVerify={verifyLedger}
          verifying={verifying}
        />
      </Section>

      <div className="grid grid-cols-1 gap-6 xl:grid-cols-2">
        <Section title="Recent orders">
          <OrdersTable orders={overview.orders.orders} />
        </Section>
        <Section title="Recent traces">
          <TracesTable traces={overview.traces.traces} />
        </Section>
      </div>

      <Section
        title="Negotiations"
        description="Every counter-offer conversation between a buyer agent and the merchant agent."
      >
        {negotiationsError && !negotiations && (
          <p className="text-sm text-err">{negotiationsError}</p>
        )}
        {negotiations && <NegotiationsPanel negotiations={negotiations} />}
      </Section>

      <Section title="Live events" description="Server-sent event bus, newest first.">
        <EventFeed />
      </Section>
    </div>
  );
}

function errorMessage(e: unknown): string {
  if (e instanceof ApiError) return e.message;
  if (e instanceof Error) return e.message;
  return "Unknown error";
}
