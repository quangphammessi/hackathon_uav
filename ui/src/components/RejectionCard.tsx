import type { CandidateAssessment, RejectionResult } from "@/lib/api";
import { StatusBadge } from "@/components/StatusBadge";
import { ClaimsTable, IntentPanel, RequirementList } from "@/components/evidence";

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

      {rejection.intent && (
        <Section title="Decoded intent">
          <IntentPanel intent={rejection.intent} />
        </Section>
      )}

      {rejection.unmet_requirements.length > 0 && (
        <Section title="Requirements no candidate could satisfy">
          <RequirementList items={rejection.unmet_requirements} />
        </Section>
      )}

      {rejection.considered.length > 0 && (
        <Section title={`Candidates considered (${rejection.considered.length})`}>
          <div className="space-y-2">
            {rejection.considered.map((c) => (
              <CandidateRow key={c.sku} candidate={c} />
            ))}
          </div>
        </Section>
      )}
    </div>
  );
}

function CandidateRow({ candidate }: { candidate: CandidateAssessment }) {
  return (
    <details className="rounded-md border border-border-soft bg-bg">
      <summary className="flex cursor-pointer select-none flex-wrap items-center gap-2 px-3 py-2 text-xs">
        <StatusBadge tone={candidate.eligible ? "ok" : "err"}>
          {candidate.eligible ? "eligible" : "disqualified"}
        </StatusBadge>
        <span className="font-medium text-text">{candidate.name}</span>
        <span className="font-mono text-text-faint">{candidate.sku}</span>
        <span className="ml-auto font-mono text-text-dim">
          fit {candidate.fit_score.toFixed(3)} &middot; similarity {candidate.similarity.toFixed(3)}
        </span>
      </summary>
      <div className="space-y-3 border-t border-border-soft p-3">
        {candidate.disqualified_by && (
          <div className="text-xs text-err">
            disqualified by: <span className="font-medium">{candidate.disqualified_by}</span>
          </div>
        )}
        {candidate.list_price !== null && (
          <div className="font-mono text-xs text-text-faint">
            list price {candidate.list_price.toFixed(2)}
          </div>
        )}
        {candidate.matched.length > 0 && (
          <div>
            <h6 className="mb-1 text-[11px] font-semibold text-text-faint">matched</h6>
            <RequirementList items={candidate.matched} />
          </div>
        )}
        {candidate.unmet.length > 0 && (
          <div>
            <h6 className="mb-1 text-[11px] font-semibold text-text-faint">unmet</h6>
            <RequirementList items={candidate.unmet} />
          </div>
        )}
        {candidate.claims.length > 0 && (
          <div>
            <h6 className="mb-1 text-[11px] font-semibold text-text-faint">values claims</h6>
            <ClaimsTable claims={candidate.claims} />
          </div>
        )}
      </div>
    </details>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="mt-5 border-t border-err/20 pt-4">
      <h4 className="mb-3 text-sm font-semibold text-text-dim">{title}</h4>
      {children}
    </div>
  );
}
