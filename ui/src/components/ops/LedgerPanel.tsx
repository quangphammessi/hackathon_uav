import type { LedgerDetail, LedgerSummary } from "@/lib/api";

export function LedgerPanel({
  summary,
  detail,
  onVerify,
  verifying,
}: {
  summary: LedgerSummary;
  detail: LedgerDetail | null;
  onVerify: () => void;
  verifying: boolean;
}) {
  return (
    <div className="space-y-4">
      <div
        className={`flex flex-wrap items-center justify-between gap-3 rounded-lg border p-4 ${
          summary.intact ? "border-ok/30 bg-ok/5" : "border-err/30 bg-err/5"
        }`}
      >
        <div>
          <div
            className={`text-lg font-semibold tracking-tight ${
              summary.intact ? "text-ok" : "text-err"
            }`}
          >
            {summary.intact ? "HASH CHAIN INTACT" : "HASH CHAIN BROKEN"}
          </div>
          {summary.error && (
            <p className="mt-1 text-xs text-err">{summary.error}</p>
          )}
          <div className="mt-1 flex gap-4 font-mono text-xs text-text-dim">
            <span>entries {summary.stats.entries}</span>
            <span>issued {summary.stats.issued}</span>
            <span>revoked {summary.stats.revoked}</span>
          </div>
        </div>
        <button
          type="button"
          onClick={onVerify}
          disabled={verifying}
          className="rounded-md border border-accent/40 bg-accent/10 px-3 py-1.5 text-xs font-medium text-accent transition-colors hover:bg-accent/20 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {verifying ? "Verifying…" : "Verify now"}
        </button>
      </div>

      {detail && (
        <div className="overflow-x-auto rounded-lg border border-border scrollbar-thin">
          <table className="w-full min-w-[640px] text-left text-xs">
            <thead>
              <tr className="border-b border-border-soft text-text-faint">
                <th className="px-3 py-2 font-medium">Seq</th>
                <th className="px-3 py-2 font-medium">Event</th>
                <th className="px-3 py-2 font-medium">Token</th>
                <th className="px-3 py-2 font-medium">Prev hash</th>
                <th className="px-3 py-2 font-medium">Entry hash</th>
              </tr>
            </thead>
            <tbody>
              {detail.entries.map((e) => (
                <tr key={e.seq} className="border-b border-border-soft last:border-0">
                  <td className="px-3 py-2 font-mono">{e.seq}</td>
                  <td className="px-3 py-2 font-mono text-text-dim">{e.event_type}</td>
                  <td className="px-3 py-2 font-mono text-text-dim">{e.token_id}</td>
                  <td className="px-3 py-2 font-mono text-text-faint">
                    {truncateHash(e.prev_hash)}
                  </td>
                  <td className="px-3 py-2 font-mono text-text-faint">
                    {truncateHash(e.entry_hash)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function truncateHash(hash: string): string {
  if (hash.length <= 16) return hash;
  return `${hash.slice(0, 8)}…${hash.slice(-6)}`;
}
