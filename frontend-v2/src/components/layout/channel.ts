/**
 * Channel indicator (P2 „SIGNAL", ui-redesign-v3) — shared by
 * TopBar (desktop) and MobileNav (mobile). Every screen carries a persistent
 * channel id; daily destinations get fixed channels, everything else CH05.
 */
const CH_MAP: [prefix: string, ch: string, page: string][] = [
  ["/tasks", "CH02", "TASKS"],
  ["/agents", "CH03", "AGENTS"],
  ["/sessions", "CH04", "SESSIONS"],
  ["/", "CH01", "COMMAND DECK"],
];

const PAGE_NAMES: [prefix: string, page: string][] = [
  ["/inbox", "INBOX"],
  ["/insights", "INSIGHTS"],
  ["/memory", "MEMORY"],
  ["/files", "FILES"],
  ["/office", "OFFICE"],
  ["/repos", "REPOS"],
  ["/skills", "SKILLS"],
  ["/runtimes", "RUNTIMES"],
  ["/loops", "LOOPS"],
  ["/schedule", "SCHEDULE"],
  ["/settings", "SETTINGS"],
  ["/content", "CONTENT"],
  ["/news", "NEWS"],
  ["/bench", "BENCHMARK"],
  ["/setup", "SETUP"],
];

export function channelFor(pathname: string): { ch: string; page: string } {
  for (const [prefix, ch, page] of CH_MAP) {
    if (prefix === "/" ? pathname === "/" : pathname.startsWith(prefix)) {
      return { ch, page };
    }
  }
  const named = PAGE_NAMES.find(([p]) => pathname.startsWith(p));
  return { ch: "CH05", page: named ? named[1] : "INDEX" };
}
