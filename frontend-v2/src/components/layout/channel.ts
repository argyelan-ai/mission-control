/**
 * Page name lookup — used by TopBar (desktop) for the instrument strip.
 * The former "MC/OS" wordmark and CH-channel chips were removed 2026-07
 * (no informational value); the brand lives in the Sidebar and the
 * MobileNav home link.
 */
const PAGE_NAMES: [prefix: string, page: string][] = [
  ["/tasks", "TASKS"],
  ["/agents", "AGENTS"],
  ["/sessions", "SESSIONS"],
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

export function pageNameFor(pathname: string): string {
  if (pathname === "/") return "COMMAND DECK";
  const named = PAGE_NAMES.find(([p]) => pathname.startsWith(p));
  return named ? named[1] : "INDEX";
}
