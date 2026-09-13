import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: "AgentMarket OS",
  description:
    "A B2A commerce platform that sells to autonomous AI shopping agents.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body className="antialiased bg-bg text-text">
        <div className="flex min-h-dvh flex-col">
          <header className="border-b border-border-soft">
            <div className="mx-auto flex max-w-[1400px] items-center justify-between gap-4 px-4 py-3 sm:px-6">
              <Link href="/" className="flex items-center gap-2">
                <span
                  aria-hidden
                  className="inline-block h-2 w-2 rounded-full bg-accent shadow-[0_0_10px_var(--accent)]"
                />
                <span className="text-sm font-semibold tracking-tight">
                  AgentMarket <span className="text-text-dim">OS</span>
                </span>
              </Link>
              <nav className="flex items-center gap-1 text-sm">
                <NavLink href="/">Agent Demo</NavLink>
                <NavLink href="/ops">Ops Dashboard</NavLink>
              </nav>
            </div>
          </header>
          <main className="mx-auto w-full max-w-[1400px] flex-1 px-4 py-6 sm:px-6">
            {children}
          </main>
          <footer className="border-t border-border-soft px-4 py-4 text-xs text-text-faint sm:px-6">
            UAVS Hackathon 2026 &middot; The B2A Shift &middot; FPT Australasia
          </footer>
        </div>
      </body>
    </html>
  );
}

function NavLink({ href, children }: { href: string; children: React.ReactNode }) {
  return (
    <Link
      href={href}
      className="rounded-md px-3 py-1.5 text-text-dim transition-colors hover:bg-bg-raised hover:text-text"
    >
      {children}
    </Link>
  );
}
