import { payableAmount, type OfferResult } from "@/lib/api";
import { StatusBadge } from "@/components/StatusBadge";
import {
  ClaimsTable,
  GroundingBadge,
  IntentPanel,
  RequirementList,
} from "@/components/evidence";

export function OfferCard({ offer }: { offer: OfferResult }) {
  const trustTone = offer.trust_status === "PASS" ? "ok" : "err";
  const payable = payableAmount(offer);
  const rationale = offer.rationale;
  const bundle = offer.bundle;

  return (
    <div className="rounded-lg border border-border bg-bg-panel p-5">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <div className="font-mono text-xs text-text-dim">{offer.sku}</div>
          <h3 className="mt-1 text-lg font-semibold">{offer.name}</h3>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {offer.negotiable && (
            <StatusBadge tone="accent">negotiable</StatusBadge>
          )}
          <StatusBadge tone="accent">OFFER</StatusBadge>
        </div>
      </div>

      <div className="mt-4 flex flex-wrap items-end gap-2">
        <span className="font-mono text-3xl font-semibold tracking-tight">
          {offer.price.currency} {payable.toFixed(2)}
        </span>
        {bundle ? (
          <span className="mb-1 font-mono text-xs text-text-faint">
            {bundle.items.length}-item kit &middot; item alone{" "}
            {offer.price.amount.toFixed(2)} &middot; fair value{" "}
            {offer.price.fair_value.toFixed(2)}
          </span>
        ) : (
          <span className="mb-1 font-mono text-xs text-text-faint">
            fair value {offer.price.fair_value.toFixed(2)} &middot; spread{" "}
            {(offer.price.spread * 100).toFixed(0)}%
          </span>
        )}
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
        {offer.negotiation_id && (
          <div className="col-span-2 sm:col-span-1">
            <dt className="text-text-faint">negotiation_id</dt>
            <dd className="truncate text-text">{offer.negotiation_id}</dd>
          </div>
        )}
      </dl>

      {offer.intent && (
        <Section title="Decoded intent">
          <IntentPanel intent={offer.intent} />
        </Section>
      )}

      {rationale && (
        <Section title="Justification">
          <div className="space-y-3">
            <GroundingBadge grounding={rationale.grounding} />
            <p className="text-sm text-text-dim">{rationale.summary}</p>

            {rationale.matched.length > 0 && (
              <Subsection title="Requirement evidence">
                <RequirementList items={rationale.matched} />
              </Subsection>
            )}

            {rationale.tradeoffs.length > 0 && (
              <Subsection title="Tradeoffs (honest, not hidden)">
                <ul className="ml-4 list-disc space-y-1 text-xs text-warn">
                  {rationale.tradeoffs.map((t, i) => (
                    <li key={i}>{t}</li>
                  ))}
                </ul>
              </Subsection>
            )}

            {rationale.verified_claims.length > 0 && (
              <Subsection title="Verified values claims">
                <ClaimsTable claims={rationale.verified_claims} />
              </Subsection>
            )}

            {rationale.rejected_alternatives.length > 0 && (
              <Subsection title="Rejected alternatives">
                <ul className="space-y-1 text-xs">
                  {rationale.rejected_alternatives.map((r) => (
                    <li
                      key={r.sku}
                      className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-border-soft bg-bg px-2.5 py-1.5"
                    >
                      <span>
                        <span className="text-text">{r.name}</span>{" "}
                        <span className="font-mono text-text-faint">{r.sku}</span>
                      </span>
                      <span className="text-text-dim">failed: {r.reason}</span>
                    </li>
                  ))}
                </ul>
              </Subsection>
            )}
          </div>
        </Section>
      )}

      {bundle && (
        <Section title="Kit breakdown">
          <div className="space-y-3">
            <div className="overflow-x-auto rounded-lg border border-border scrollbar-thin">
              <table className="w-full min-w-[640px] text-left text-xs">
                <thead>
                  <tr className="border-b border-border-soft text-text-faint">
                    <th className="px-3 py-2 font-medium">Item</th>
                    <th className="px-3 py-2 font-medium">Role in kit</th>
                    <th className="px-3 py-2 font-medium">Price</th>
                    <th className="px-3 py-2 font-medium">Trust</th>
                  </tr>
                </thead>
                <tbody>
                  {bundle.items.map((it) => (
                    <tr key={it.sku} className="border-b border-border-soft align-top last:border-0">
                      <td className="px-3 py-2">
                        <div className="text-text">{it.name}</div>
                        <div className="font-mono text-[10px] text-text-faint">{it.sku}</div>
                      </td>
                      <td className="px-3 py-2 text-text-dim">{it.role_in_bundle}</td>
                      <td className="px-3 py-2 font-mono text-text">
                        {it.price.currency} {it.price.amount.toFixed(2)}
                      </td>
                      <td className="px-3 py-2">
                        <StatusBadge tone={it.trust_status === "PASS" ? "ok" : "err"}>
                          {it.trust_status}
                        </StatusBadge>
                      </td>
                    </tr>
                  ))}
                </tbody>
                <tfoot>
                  <tr className="border-t border-border-soft font-mono">
                    <td className="px-3 py-2 text-text-faint" colSpan={2}>
                      subtotal
                    </td>
                    <td className="px-3 py-2 text-text" colSpan={2}>
                      {bundle.currency} {bundle.subtotal.toFixed(2)}
                    </td>
                  </tr>
                  <tr className="font-mono">
                    <td className="px-3 py-2 text-text-faint" colSpan={2}>
                      bundle discount
                    </td>
                    <td className="px-3 py-2 text-ok" colSpan={2}>
                      -{bundle.currency} {bundle.bundle_discount.toFixed(2)}
                    </td>
                  </tr>
                  <tr className="font-mono">
                    <td className="px-3 py-2 font-semibold text-text-faint" colSpan={2}>
                      total
                    </td>
                    <td className="px-3 py-2 text-lg font-semibold text-text" colSpan={2}>
                      {bundle.currency} {bundle.total.toFixed(2)}
                    </td>
                  </tr>
                </tfoot>
              </table>
            </div>

            {bundle.guardrails_applied.length > 0 && (
              <div className="flex flex-wrap gap-2">
                {bundle.guardrails_applied.map((g) => (
                  <StatusBadge key={g} tone="warn">
                    {g}
                  </StatusBadge>
                ))}
              </div>
            )}

            {bundle.dropped.length > 0 && (
              <Subsection title="Considered and dropped from the kit">
                <ul className="space-y-1 text-xs">
                  {bundle.dropped.map((d) => (
                    <li
                      key={d.sku}
                      className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-border-soft bg-bg px-2.5 py-1.5"
                    >
                      <span>
                        <span className="text-text">{d.name}</span>{" "}
                        <span className="font-mono text-text-faint">{d.sku}</span>
                      </span>
                      <span className="text-text-dim">{d.reason}</span>
                    </li>
                  ))}
                </ul>
              </Subsection>
            )}
          </div>
        </Section>
      )}

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

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="mt-5 border-t border-border-soft pt-4">
      <h4 className="mb-3 text-sm font-semibold text-text-dim">{title}</h4>
      {children}
    </div>
  );
}

function Subsection({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div>
      <h5 className="mb-1.5 text-xs font-semibold text-text-faint">{title}</h5>
      {children}
    </div>
  );
}
