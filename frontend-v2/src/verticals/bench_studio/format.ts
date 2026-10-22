// Numbers and dates on the bench pages (round-1 user test, P4): one form per
// value, in the UI language. Durations use the app's duration form (K17:
// "8 min", "1 h 32 min" — never "532 s"), token counts the locale's digit
// grouping (no "39.2k" next to "33.328"), and the start time the viewer's own
// clock, not UTC.
import { formatDuration, parseTs } from "@/lib/taskDetail/format";

/** "8 min" / "1 h 32 min" / "45 s" for a run that took `ms` milliseconds. */
export function runDuration(ms: number | null | undefined, locale: string): string | null {
  if (ms == null || Number.isNaN(ms) || ms <= 0) return null;
  return formatDuration(Math.round(ms / 1000), locale);
}

/** 39200 -> "39,200" (en) / "39.200" (de) — the same grouping the effort line uses. */
export function tokenCount(n: number, locale: string): string {
  return n.toLocaleString(locale);
}

/** When a challenge was started, short enough for a list row:
 *  "Oct 23, 00:19" (en) / "23. Okt., 00:19" (de), in the viewer's time zone. */
export function startedAt(iso: string | null | undefined, locale: string, timeZone?: string): string | null {
  const d = parseTs(iso);
  if (!d) return null;
  return new Intl.DateTimeFormat(locale, {
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
    ...(timeZone ? { timeZone } : {}),
  }).format(d);
}
