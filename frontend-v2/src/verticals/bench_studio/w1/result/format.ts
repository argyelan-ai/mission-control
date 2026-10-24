// Number and duration formats of the bench result page and the X-Kit sheet
// (package C). Pure functions — the words around them come from i18n.
//
// Class A compute time is a clock ("1:06"), the same form the public card
// prints, because a benchmark reader compares seconds. Class B work time is
// minutes ("92 min"): an agent run is long, a clock would read as a timer.
// Numbers are tabular sans in the UI (K7), never mono.
import type { Stat } from "../types";

/** The app's number locale: Swiss German or British English (as elsewhere). */
export function numLocale(locale: string): string {
  return locale === "de" ? "de-CH" : "en-GB";
}

/** m:ss, or h:mm:ss from one hour on. */
export function formatClock(ms: number): string {
  const total = Math.floor(ms / 1000);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const ss = String(s).padStart(2, "0");
  if (h > 0) return `${h}:${String(m).padStart(2, "0")}:${ss}`;
  return `${m}:${ss}`;
}

/** "45 s", "92 min", "1 h 56 min" — minutes up to 99, then hours. */
export function formatWork(ms: number): string {
  const totalS = Math.floor(ms / 1000);
  if (totalS < 60) return `${totalS} s`;
  const min = Math.floor(totalS / 60);
  if (min < 100) return `${min} min`;
  const h = Math.floor(min / 60);
  const rest = min % 60;
  return rest ? `${h} h ${rest} min` : `${h} h`;
}

export function formatTime(ms: number, kind: "gen_ms" | "active_ms"): string {
  return kind === "gen_ms" ? formatClock(ms) : formatWork(ms);
}

function num(n: number, locale: string, digits: number): string {
  return new Intl.NumberFormat(locale, {
    maximumFractionDigits: digits,
    minimumFractionDigits: 0,
  }).format(n);
}

/** 980 · 6.6k · 264k · 1.12 M */
export function formatTokens(n: number, locale: string): string {
  if (n < 1000) return num(Math.round(n), locale, 0);
  if (n < 100_000) return `${num(Math.round(n / 100) / 10, locale, 1)}k`;
  if (n < 1_000_000) return `${num(Math.round(n / 1000), locale, 0)}k`;
  return `${num(Math.round(n / 10_000) / 100, locale, 2)} M`;
}

/** 410 ms → "0.4 s" */
export function formatSeconds(ms: number, locale: string): string {
  return `${num(Math.round(ms / 100) / 10, locale, 1)} s`;
}

/** Dollar amount for the "${usd}" words — two decimals, never "0.00". */
export function formatUsd(usd: number): string {
  return Math.max(usd, 0.01).toFixed(2);
}

/** 17 kB · 18.8 MB */
export function formatBytes(bytes: number, locale: string): string {
  if (bytes < 1_000_000) return `${num(Math.round(bytes / 1000), locale, 0)} kB`;
  return `${num(Math.round(bytes / 100_000) / 10, locale, 1)} MB`;
}

/** Plain integer with the locale's grouping ("16,384" / "16’384"). */
export function formatInt(n: number, locale: string): string {
  return num(Math.round(n), locale, 0);
}

export interface StatParts {
  median: string;
  /** null when the range collapses to one value (n = 1 or all equal). */
  min: string | null;
  max: string | null;
  n: number;
}

/** Median + range as text; null when the number was not measured. */
export function statText(stat: Stat | null | undefined, fmt: (v: number) => string): StatParts | null {
  if (!stat || !stat.measured || stat.median === null) return null;
  const median = fmt(stat.median);
  const lo = stat.min !== null ? fmt(stat.min) : null;
  const hi = stat.max !== null ? fmt(stat.max) : null;
  const ranged = stat.n > 1 && lo !== null && hi !== null && lo !== hi;
  return { median, min: ranged ? lo : null, max: ranged ? hi : null, n: stat.n };
}

/** "22.10." / "22/10" — the day of a test, short. */
export function formatDay(iso: string, locale: string): string {
  return new Intl.DateTimeFormat(locale, { day: "2-digit", month: "2-digit" }).format(new Date(iso));
}

/** "22.10.2026" / "22/10/2026" */
export function formatDate(iso: string, locale: string): string {
  return new Intl.DateTimeFormat(locale, {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
  }).format(new Date(iso));
}

/** "16:36" */
export function formatClockTime(iso: string, locale: string): string {
  return new Intl.DateTimeFormat(locale, { hour: "2-digit", minute: "2-digit" }).format(new Date(iso));
}

/** The 7-character short form of a sha (receipt, settings text). */
export function shortSha(sha: string): string {
  return sha.slice(0, 7);
}
