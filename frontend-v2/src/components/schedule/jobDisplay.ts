/**
 * How a scheduled job's times and states read — one place for the list row,
 * the phone card and the detail page (round-3 user test t4 S8).
 *
 * A paused job (`enabled: false`) keeps the `next_run_at` it had when it was
 * switched off; that time is in the past and will never come. It read
 * "Next: 24d ago" / "Mon 28.09., 07:00". A paused job has no next run: it
 * says so ("— (paused)").
 */
import type { ScheduledJob } from "@/lib/types";

type Translate = (key: string, values?: Record<string, string | number>) => string;

/** Relative time in the list's short forms ("in 3h", "2d ago"). */
export function relativeJobTime(iso: string | null, t: Translate, now: number = Date.now()): string {
  if (!iso) return "—";
  const ts = new Date(iso).getTime();
  if (Number.isNaN(ts)) return "—";
  const diff = now - ts;
  if (diff < 0) {
    const mAhead = Math.floor(-diff / 60000);
    if (mAhead < 60) return t("inM", { count: mAhead });
    const hAhead = Math.floor(mAhead / 60);
    if (hAhead < 48) return t("inH", { count: hAhead });
    return t("inD", { count: Math.floor(hAhead / 24) });
  }
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return t("justNow");
  if (mins < 60) return t("mAgo", { count: mins });
  const hours = Math.floor(mins / 60);
  if (hours < 48) return t("hAgo", { count: hours });
  return t("dAgo", { count: Math.floor(hours / 24) });
}

/** The list's "next" value: relative, or "— (paused)" for a paused job. */
export function nextRunRelative(job: ScheduledJob, t: Translate, now: number = Date.now()): string {
  if (!job.enabled) return t("nextPaused");
  return relativeJobTime(job.next_run_at, t, now);
}

/** The detail page's "next run" value: a date, or "— (paused)". */
export function nextRunAbsolute(job: ScheduledJob, t: Translate, dateLocale: string): string {
  if (!job.enabled) return t("nextPaused");
  if (!job.next_run_at) return "—";
  return new Date(job.next_run_at).toLocaleString(dateLocale, {
    weekday: "short",
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

const RUN_STATUSES = new Set(["success", "failed", "running", "skipped"]);

/** A run status in the UI language ("success" → "Succeeded" / "Erfolgreich"). */
export function runStatusLabel(status: string | null | undefined, t: Translate): string {
  if (!status) return "—";
  return RUN_STATUSES.has(status) ? t(`runStatus.${status}`) : status;
}

/** A chart day ("2026-09-23") in the UI's date form: "23.09." / "23/09". */
export function formatChartDay(day: string, dateLocale: string): string {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(day);
  if (!m) return day;
  // Noon UTC: the calendar day stays the same in every time zone.
  const d = new Date(Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3]), 12));
  return d.toLocaleDateString(dateLocale, { day: "2-digit", month: "2-digit", timeZone: "UTC" });
}
