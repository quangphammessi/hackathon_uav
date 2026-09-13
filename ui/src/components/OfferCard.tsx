import type { OfferResult } from "@/lib/api";
import { StatusBadge } from "@/components/StatusBadge";

export function OfferCard({ offer }: { offer: OfferResult }) {
  const trustTone = offer.trust_status === "PASS" ? "ok" : "err";
  return (
    <div className="rounded-lg border border-border bg-bg-panel p-5">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <div className="font-mono text-xs text-text-dim">{offer.sku}</div>
          <h3 className="mt-1 text-lg font-semibold">{offer.name}</h3>
        </div>
        <StatusBadge tone="accent">OFFER</StatusBadge>
      </div>

      <div className="mt-4 flex items-end gap-2">
        <span className="font-mono text-3xl font-semibold tracking-tight">
          {offer.price.currency} {offer.price.amount.toFixed(2)}
        </span>
        <span className="mb-1 font-mono text-xs text-text-faint">
          fair value {offer.price.fair_value.toFixed(2)} &middot; spread{" "}
          {(offer.price.spread * 100).toFixed(0)}%
        </span>
      </div>

      <div className="mt-4 flex flex-wrap items-center gap-2">
        <StatusBadge tone={trustTone === "ok" ? "ok" : "err"}>
          trust {offer.trust_status} &middot; {(offer.trust_confidence * 100).toFixed(0)}%
        </StatusBadge>
        {offer.price.guardrails_applied.length === 0 ? (
          <StatusBadge tone="neutral">no guardrails triggered</StatusBadge>
        ) : (
          offer.price.guardrails_applied.map((g) => (
            <StatusBadge key={g} tone="warn">
              {g}
            </StatusBadge>
          ))
        )}
      </div>

      <dl className="mt-4 grid grid-cols-2 gap-x-4 gap-y-1.5 font-mono text-xs text-text-dim sm:grid-cols-3">
        <div>
          <dt className="text-text-faint">quote_id</dt>
          <dd className="text-text">{offer.price.quote_id}</dd>
        </div>
        <div>
          <dt className="text-text-faint">offer_id</dt>
          <dd className="text-text">{offer.offer_id}</dd>
        </div>
        <div className="col-span-2 sm:col-span-1">
          <dt className="text-text-faint">trust_token_ref</dt>
          <dd className="truncate text-text">{offer.trust_token_ref}</dd>
        </div>
      </dl>

      {Object.keys(offer.attributes).length > 0 && (
        <details className="mt-4 text-xs text-text-dim">
          <summary className="cursor-pointer select-none text-text-faint hover:text-text">
            product attributes
          </summary>
          <pre className="mt-2 overflow-x-auto rounded-md border border-border-soft bg-bg p-3 font-mono text-[11px] leading-relaxed text-text scrollbar-thin">
            {JSON.stringify(offer.attributes, null, 2)}
          </pre>
        </details>
      )}
    </div>
  );
}
