import type { RejectionResult } from "@/lib/api";
import { StatusBadge } from "@/components/StatusBadge";

const REASON_COPY: Record<string, string> = {
  CHAIN_GAP:
    "The product's provenance event chain is missing a required step. Rather than sell an unverifiable item, the verification gate refused the offer.",
  NO_CANDIDATES:
    "No catalog item satisfied the request after the bounded repair/retry budget was exhausted. The agent returned a clean rejection instead of hallucinating a match.",
};

export function RejectionCard({ rejection }: { rejection: RejectionResult }) {
  const copy = REASON_COPY[rejection.reason_code];
  return (
    <div className="rounded-lg border border-err/30 bg-err/5 p-5">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <div className="font-mono text-xs text-text-dim">
            {rejection.sku ?? "no sku matched"}
          </div>
          <h3 className="mt-1 text-lg font-semibold text-err">Rejected by design</h3>
        </div>
        <StatusBadge tone="err">REJECTED</StatusBadge>
      </div>

      <div className="mt-4 font-mono text-xl font-semibold text-err">
        {rejection.reason_code}
      </div>
      {copy && <p className="mt-2 max-w-2xl text-sm text-text-dim">{copy}</p>}

      <div className="mt-4 space-y-1 font-mono text-xs">
        <div className="text-text-faint">detail</div>
        <div className="rounded-md border border-border-soft bg-bg p-3 text-text-dim">
          {rejection.detail}
        </div>
      </div>

      <div className="mt-3 font-mono text-xs text-text-faint">
        query: <span className="text-text-dim">&ldquo;{rejection.query}&rdquo;</span>
      </div>
    </div>
  );
}
