// Shared presentational pieces for rendering decoded intent and justification
// evidence -- used by both OfferCard (a successful match) and RejectionCard
// (a refusal), since both now carry an IntentPlan and RequirementMatch[]s.

import type {
  ClaimVerification,
  GroundingReport,
  IntentPlan,
  RequirementMatch,
} from "@/lib/api";
import { StatusBadge } from "@/components/StatusBadge";

export function fmtValue(value: unknown): string {
  if (Array.isArray(value)) return value.map((v) => String(v)).join(", ");
  if (typeof value === "boolean") return value ? "yes" : "no";
  if (value === null || value === undefined) return "--";
  return String(value);
}

export function claimTone(status: ClaimVerification["status"]): "ok" | "warn" | "neutral" {
  if (status === "VERIFIED") return "ok";
  if (status === "ASSERTED_UNATTESTED") return "warn";
  return "neutral";
}

export function groundingTone(status: GroundingReport["status"]): "ok" | "warn" | "neutral" {
  if (status === "VERIFIED") return "ok";
  if (status === "TEMPLATE_FALLBACK") return "warn";
  return "neutral";
}

/** The decoded intent: the one-sentence readback plus every predicate next
 *  to the exact words in the buyer's request that produced it. This is the
 *  single most important proof surface in the whole app. */
export function IntentPanel({ intent }: { intent: IntentPlan }) {
  return (
    <div className="space-y-3">
      <p className="text-sm text-text">{intent.interpreted_need}</p>
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 font-mono text-[11px] text-text-faint">
        <span>
          decoded_by <span className="text-text-dim">{intent.decoded_by}</span>
        </span>
        {intent.budget !== null && (
          <span>
            budget{" "}
            <span className="text-text-dim">
              {intent.budget.toFixed(2)} ({intent.budget_is_hard ? "hard" : "soft"})
            </span>
          </span>
        )}
        {intent.recipient && (
          <span>
            recipient <span className="text-text-dim">{intent.recipient}</span>
          </span>
        )}
        {intent.bundle_intent && <StatusBadge tone="accent">kit requested</StatusBadge>}
      </div>
      {intent.constraints.length === 0 ? (
        <p className="text-xs text-text-faint">No constraints decoded.</p>
      ) : (
        <div className="overflow-x-auto rounded-lg border border-border scrollbar-thin">
          <table className="w-full min-w-[600px] text-left text-xs">
            <thead>
              <tr className="border-b border-border-soft text-text-faint">
                <th className="px-3 py-2 font-medium">Predicate</th>
                <th className="px-3 py-2 font-medium">Kind</th>
                <th className="px-3 py-2 font-medium">From the buyer&apos;s words</th>
                <th className="px-3 py-2 font-medium">Why</th>
              </tr>
            </thead>
            <tbody>
              {intent.constraints.map((c, i) => (
                <tr key={i} className="border-b border-border-soft align-top last:border-0">
                  <td className="whitespace-nowrap px-3 py-2 font-mono text-text">
                    {c.field} {c.op} {fmtValue(c.value)}
                  </td>
                  <td className="px-3 py-2">
                    <StatusBadge tone={c.kind === "hard" ? "accent" : "neutral"}>
                      {c.kind}
                    </StatusBadge>
                  </td>
                  <td className="px-3 py-2 italic text-text-dim">
                    &ldquo;{c.source_phrase}&rdquo;
                  </td>
                  <td className="px-3 py-2 text-text-faint">{c.rationale || "--"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

/** A list of requirement<->evidence pairs, each carrying satisfied/kind and
 *  the source phrase that produced the requirement in the first place. */
export function RequirementList({ items }: { items: RequirementMatch[] }) {
  if (items.length === 0) {
    return <p className="text-xs text-text-faint">none recorded</p>;
  }
  return (
    <ul className="space-y-1.5">
      {items.map((m, i) => (
        <li key={i} className="rounded-md border border-border-soft bg-bg p-2.5 text-xs">
          <div className="flex flex-wrap items-center gap-1.5">
            <StatusBadge tone={m.satisfied ? "ok" : "err"}>
              {m.satisfied ? "met" : "unmet"}
            </StatusBadge>
            <StatusBadge tone={m.kind === "hard" ? "accent" : "neutral"}>{m.kind}</StatusBadge>
            <span className="font-medium text-text">{m.requirement}</span>
          </div>
          <div className="mt-1 text-text-dim">{m.evidence}</div>
          <div className="mt-1 font-mono text-[10px] text-text-faint">
            from &ldquo;{m.source_phrase}&rdquo;
          </div>
        </li>
      ))}
    </ul>
  );
}

/** VERIFIED / ASSERTED_UNATTESTED / NOT_CLAIMED per values claim, with the
 *  attesting body and certificate id -- the "greenwashing detector". */
export function ClaimsTable({ claims }: { claims: ClaimVerification[] }) {
  if (claims.length === 0) {
    return <p className="text-xs text-text-faint">no claims checked</p>;
  }
  return (
    <div className="overflow-x-auto rounded-lg border border-border scrollbar-thin">
      <table className="w-full min-w-[520px] text-left text-xs">
        <thead>
          <tr className="border-b border-border-soft text-text-faint">
            <th className="px-3 py-2 font-medium">Claim</th>
            <th className="px-3 py-2 font-medium">Status</th>
            <th className="px-3 py-2 font-medium">Attested by</th>
            <th className="px-3 py-2 font-medium">Certificate</th>
          </tr>
        </thead>
        <tbody>
          {claims.map((c) => (
            <tr key={c.claim} className="border-b border-border-soft last:border-0">
              <td className="px-3 py-2 text-text">{c.label}</td>
              <td className="px-3 py-2">
                <StatusBadge tone={claimTone(c.status)}>{c.status}</StatusBadge>
              </td>
              <td className="px-3 py-2 text-text-dim">{c.attested_by ?? "--"}</td>
              <td className="px-3 py-2 font-mono text-text-faint">{c.certificate ?? "--"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Whether an LLM wrote the justification and passed its automated fact
 *  check (VERIFIED), failed and was replaced by a template
 *  (TEMPLATE_FALLBACK -- the interesting case, show the violations), or no
 *  model was consulted at all (TEMPLATE_ONLY). */
export function GroundingBadge({ grounding }: { grounding: GroundingReport }) {
  return (
    <div className="space-y-1.5">
      <StatusBadge tone={groundingTone(grounding.status)}>
        grounding {grounding.status} &middot; composed by {grounding.composed_by}
      </StatusBadge>
      {grounding.violations.length > 0 && (
        <ul className="ml-4 list-disc space-y-0.5 text-xs text-warn">
          {grounding.violations.map((v, i) => (
            <li key={i}>{v}</li>
          ))}
        </ul>
      )}
    </div>
  );
}
