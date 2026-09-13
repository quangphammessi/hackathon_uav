"use client";

import { Fragment, useState } from "react";
import type { NegotiationSummary } from "@/lib/api";
import { StatusBadge } from "@/components/StatusBadge";

function statusTone(status: string): "ok" | "warn" | "err" | "accent" | "neutral" {
  switch (status) {
    case "CONCEDED":
      return "ok";
    case "PARTIAL_CONCESSION":
    case "ALTERNATIVE_PROPOSED":
    case "BUNDLE_RESTRUCTURED":
      return "accent";
    case "HELD":
      return "warn";
    case "EXHAUSTED":
      return "err";
    default:
      return "neutral";
  }
}

export function NegotiationsPanel({
  negotiations,
}: {
  negotiations: NegotiationSummary[];
}) {
  const [expanded, setExpanded] = useState<string | null>(null);

  if (negotiations.length === 0) {
    return <p className="text-sm text-text-faint">No negotiations recorded yet.</p>;
  }

  return (
    <div className="overflow-x-auto rounded-lg border border-border scrollbar-thin">
      <table className="w-full min-w-[760px] text-left text-xs">
        <thead>
          <tr className="border-b border-border-soft text-text-faint">
            <th className="px-3 py-2 font-medium">Negotiation</th>
            <th className="px-3 py-2 font-medium">SKU</th>
            <th className="px-3 py-2 font-medium">Opening</th>
            <th className="px-3 py-2 font-medium">Current</th>
            <th className="px-3 py-2 font-medium">Status</th>
            <th className="px-3 py-2 font-medium">Rounds</th>
          </tr>
        </thead>
        <tbody>
          {negotiations.map((n) => {
            const isOpen = expanded === n.negotiation_id;
            return (
              <Fragment key={n.negotiation_id}>
                <tr
                  onClick={() =>
                    setExpanded(isOpen ? null : n.negotiation_id)
                  }
                  className="cursor-pointer border-b border-border-soft last:border-0 hover:bg-bg-raised"
                >
                  <td className="px-3 py-2 font-mono text-text-dim">
                    {n.negotiation_id}
                  </td>
                  <td className="px-3 py-2 font-mono text-text-dim">{n.sku}</td>
                  <td className="px-3 py-2 font-mono">{n.opening_amount.toFixed(2)}</td>
                  <td className="px-3 py-2 font-mono">{n.current_amount.toFixed(2)}</td>
                  <td className="px-3 py-2">
                    <StatusBadge tone={statusTone(n.status)}>{n.status}</StatusBadge>
                  </td>
                  <td className="px-3 py-2 font-mono">{n.rounds.length}</td>
                </tr>
                {isOpen && n.rounds.length > 0 && (
                  <tr className="border-b border-border-soft bg-bg last:border-0">
                    <td colSpan={6} className="px-3 py-2">
                      <ol className="space-y-1.5 border-l border-border-soft pl-3">
                        {n.rounds.map((r) => (
                          <li key={r.round} className="text-[11px]">
                            <div className="flex flex-wrap items-center gap-2 font-mono text-text-faint">
                              <span
                                className={
                                  r.actor === "buyer_agent" ? "text-accent" : "text-text-dim"
                                }
                              >
                                {r.actor}
                              </span>
                              <span>round {r.round}</span>
                              {r.proposed_amount !== null && (
                                <span>${r.proposed_amount.toFixed(2)}</span>
                              )}
                              {r.outcome && <span>{r.outcome}</span>}
                              {r.reason_code && <span>{r.reason_code}</span>}
                            </div>
                            <div className="text-text-dim">{r.message}</div>
                          </li>
                        ))}
                      </ol>
                    </td>
                  </tr>
                )}
              </Fragment>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
