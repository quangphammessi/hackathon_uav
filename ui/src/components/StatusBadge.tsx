const TONES = {
  ok: "bg-ok/10 text-ok border-ok/30",
  warn: "bg-warn/10 text-warn border-warn/30",
  err: "bg-err/10 text-err border-err/30",
  neutral: "bg-bg-raised text-text-dim border-border",
  accent: "bg-accent/10 text-accent border-accent/30",
} as const;

export function StatusBadge({
  tone = "neutral",
  children,
}: {
  tone?: keyof typeof TONES;
  children: React.ReactNode;
}) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-medium ${TONES[tone]}`}
    >
      {children}
    </span>
  );
}

export function Dot({ tone = "neutral" }: { tone?: keyof typeof TONES }) {
  const color = {
    ok: "bg-ok",
    warn: "bg-warn",
    err: "bg-err",
    neutral: "bg-text-faint",
    accent: "bg-accent",
  }[tone];
  return <span aria-hidden className={`inline-block h-2 w-2 rounded-full ${color}`} />;
}
