// Numbers, durations and dates for the W1 test sheet (P3) and live run (P4).
// Package B's own copy (the legacy bench `format.ts` is not imported, see
// CONTRACT.md §4). One form per value, in the UI language:
//   - a finished run time is a measurement: "45 s", "1 min 6 s" (K17 — never
//     "1:06" for a closed duration); from an hour on the app's duration form;
//   - a running clock is a stopwatch: "0:41", "12:05", "1:02:09";
//   - token counts use the locale's digit grouping ("6,607" / "6’607");
//   - estimates are ranges of whole minutes ("3–4 min", "4–92 min").
import { formatDuration, parseTs } from "@/lib/taskDetail/format";

/** A finished run's time from milliseconds, or null when unknown. */
export function runDuration(ms: number | null | undefined, locale: string): string | null {
  if (ms == null || Number.isNaN(ms) || ms <= 0) return null;
  const total = Math.round(ms / 1000);
  if (total >= 3600) return formatDuration(total, locale);
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  if (minutes === 0) return `${seconds} s`;
  return seconds ? `${minutes} min ${seconds} s` : `${minutes} min`;
}

/** A running clock: "0:41", "12:05", "1:02:09". */
export function stopwatch(ms: number | null | undefined): string | null {
  if (ms == null || Number.isNaN(ms) || ms < 0) return null;
  const total = Math.floor(ms / 1000);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const ss = String(s).padStart(2, "0");
  return h > 0 ? `${h}:${String(m).padStart(2, "0")}:${ss}` : `${m}:${ss}`;
}

/** 6607 -> "6,607" (en) / "6’607" (de-CH) / "6.607" (de). */
export function tokenCount(n: number, locale: string): string {
  return Math.round(n).toLocaleString(locale);
}

/** Decode speed, whole tokens per second. */
export function tokPerSec(n: number, locale: string): string {
  return Math.round(n).toLocaleString(locale);
}

/** US dollars for a price line: "0.04", "1.35", "<0.01". */
export function usd(amount: number): string {
  if (amount > 0 && amount < 0.01) return "<0.01";
  return amount.toFixed(2);
}

/** A whole-minute range: "4 min", "3–4 min", "4–92 min", "1 h 5 min" when
 *  it is one long value. Ceil for the upper bound, floor (min 1) for the
 *  lower, so a 180–260 s estimate reads "3–5 min", never "3–4" too short. */
export function minuteRange(minS: number | null, maxS: number | null): string | null {
  const lo = minS != null && minS >= 0 ? Math.max(1, Math.floor(minS / 60)) : null;
  const hi = maxS != null && maxS >= 0 ? Math.max(1, Math.ceil(maxS / 60)) : null;
  if (lo == null && hi == null) return null;
  if (lo == null || hi == null || lo === hi) {
    const v = (hi ?? lo) as number;
    return v >= 60 ? (formatDuration(v * 60) ?? `${v} min`) : `${v} min`;
  }
  return `${lo}–${hi} min`;
}

/** Short date for a stamp: "21.10." (de) / "10/21" (en). */
export function shortDate(iso: string | null | undefined, locale: string, timeZone?: string): string | null {
  const d = parseTs(iso);
  if (!d) return null;
  return new Intl.DateTimeFormat(locale, {
    day: "numeric",
    month: "numeric",
    ...(timeZone ? { timeZone } : {}),
  }).format(d);
}

/** Clock time for "startet um 20:27". */
export function clockTime(iso: string | null | undefined, locale: string, timeZone?: string): string | null {
  const d = parseTs(iso);
  if (!d) return null;
  return new Intl.DateTimeFormat(locale, {
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
    ...(timeZone ? { timeZone } : {}),
  }).format(d);
}
