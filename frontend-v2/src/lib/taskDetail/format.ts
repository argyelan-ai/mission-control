/**
 * Formatters for the task detail: durations ("3 d 2 h"), time spans and
 * money. One place so the state card, the facts row and the Summary tab all
 * say the same thing the same way.
 */

// The backend stores naive UTC (datetime.utcnow) and serialises it without a
// zone designator; `new Date("2026-09-20T10:00:00")` would read that as LOCAL
// time and be off by the UTC offset. A trailing Z / ±hh:mm is kept as-is.
const HAS_ZONE = /(Z|[+-]\d{2}:?\d{2})$/i;

export function parseTs(value: string | null | undefined): Date | null {
  if (!value) return null;
  const iso = HAS_ZONE.test(value) || !value.includes("T") ? value : `${value}Z`;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? null : d;
}

export function secondsBetween(
  start: string | null | undefined,
  end: string | null | undefined,
  now: Date = new Date(),
): number | null {
  const a = parseTs(start);
  if (!a) return null;
  const b = end ? parseTs(end) : now;
  if (!b) return null;
  return Math.round((b.getTime() - a.getTime()) / 1000);
}

const DAY_UNIT: Record<string, string> = { en: "d", de: "T" };

/** Compact duration with at most the two largest units: "3 d 2 h", "45 min". */
export function formatDuration(seconds: number | null | undefined, locale: string = "en"): string | null {
  if (seconds == null || Number.isNaN(seconds) || seconds < 0) return null;
  const s = Math.floor(seconds);
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  const dayUnit = DAY_UNIT[locale] ?? DAY_UNIT.en;
  if (d > 0) return h > 0 ? `${d} ${dayUnit} ${h} h` : `${d} ${dayUnit}`;
  if (h > 0) return m > 0 ? `${h} h ${m} min` : `${h} h`;
  if (m > 0) return `${m} min`;
  return `${s} s`;
}

/** Age of a timestamp as a compact duration ("2 d"), or null. */
export function formatAge(
  ts: string | null | undefined,
  locale: string = "en",
  now: Date = new Date(),
): string | null {
  const s = secondsBetween(ts, null, now);
  return s == null ? null : formatDuration(Math.max(0, s), locale);
}

export function formatUsd(amount: number): string {
  if (amount > 0 && amount < 0.005) return "<$0.01";
  return `$${amount.toFixed(2)}`;
}

/** Absolute timestamp for tooltips, in the viewer's locale. */
export function formatAbsolute(ts: string | null | undefined, locale: string = "en"): string {
  const d = parseTs(ts);
  if (!d) return "";
  return d.toLocaleString(locale === "de" ? "de-CH" : "en-GB", { dateStyle: "medium", timeStyle: "short" });
}
