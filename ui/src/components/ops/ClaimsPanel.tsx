"use client";

import { useState } from "react";
import type { ClaimVerification } from "@/lib/api";
import { ClaimsTable } from "@/components/evidence";

/**
 * The "greenwashing detector": look up every values claim on file for a SKU
 * and show whether it is VERIFIED against signed provenance, merely
 * ASSERTED_UNATTESTED, or NOT_CLAIMED at all.
 */
export function ClaimsPanel({
  sku,
  claims,
  loading,
  error,
  onLookup,
}: {
  sku: string | null;
  claims: ClaimVerification[] | null;
  loading: boolean;
  error: string | null;
  onLookup: (sku: string) => void;
}) {
  const [input, setInput] = useState(sku ?? "");

  return (
    <div className="space-y-3">
      <div className="flex flex-col gap-2 sm:flex-row">
        <input
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && input.trim()) onLookup(input.trim());
          }}
          placeholder="SKU, e.g. 0950600013534"
          className="flex-1 rounded-md border border-border bg-bg px-3 py-2 text-sm font-mono outline-none placeholder:text-text-faint focus:border-accent"
        />
        <button
          type="button"
          onClick={() => input.trim() && onLookup(input.trim())}
          disabled={loading || !input.trim()}
          className="rounded-md border border-accent/40 bg-accent/10 px-4 py-2 text-sm font-medium text-accent transition-colors hover:bg-accent/20 disabled:cursor-not-allowed disabled:opacity-40"
        >
          {loading ? "Looking up…" : "Look up claims"}
        </button>
      </div>
      <p className="text-xs text-text-faint">
        Click a row in the provenance table above to look up that SKU, or type
        one directly. A claim only reads VERIFIED when an independent
        auditor&rsquo;s certification event sits in that batch&rsquo;s
        provenance chain &mdash; the merchant asserting it is not evidence.
      </p>
      {error && <p className="text-xs text-err">{error}</p>}
      {claims && (
        <div>
          <div className="mb-1.5 font-mono text-xs text-text-faint">{sku}</div>
          <ClaimsTable claims={claims} />
        </div>
      )}
    </div>
  );
}
