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

const ACTION_TYPES = new Set(["create_task", "chat_send", "api_call"]);

/** A job's action in the UI language ("create_task" → "Create task"). */
export function actionTypeLabel(action: string | null | undefined, t: Translate): string {
  if (!action) return "—";
  return ACTION_TYPES.has(action) ? t(`actionType.${action}`) : action;
}

/**
 * A run duration in the UI's number form, with a space before the unit:
 * "23 ms", "1,2 s", "1,5 min" (round-4 r2 N5: "23ms"). Zero or invalid → "—".
 */
export function formatDuration(ms: number, locale: string): string {
  if (!Number.isFinite(ms) || ms <= 0) return "—";
  const one = new Intl.NumberFormat(locale, { maximumFractionDigits: 1 });
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 60_000) return `${one.format(ms / 1000)} s`;
  return `${one.format(ms / 60_000)} min`;
}

export type JobStateTone = "active" | "paused" | "snoozed";

/**
 * The detail page's state sentence under the title (K12): one state word and
 * the one time that belongs to it (K10). "Paused · last run 26 days ago",
 * "Active · next run in about 3 hours", "Snoozed until Fri 02.01., 15:00".
 * `ago` renders a relative time in the UI language (the shared `timeAgo`).
 */
export function jobStateSentence(
  job: ScheduledJob,
  t: Translate,
  ago: (iso: string) => string,
  dateLocale: string,
  now: number = Date.now(),
): { tone: JobStateTone; text: string } {
  if (!job.enabled) {
    const when = job.last_run_at ? t("stateLastRun", { ago: ago(job.last_run_at) }) : t("stateNeverRun");
    return { tone: "paused", text: `${t("paused")} · ${when}` };
  }
  const snoozed = job.snoozed_until ? new Date(job.snoozed_until).getTime() : NaN;
  if (Number.isFinite(snoozed) && snoozed > now) {
    const until = new Date(snoozed).toLocaleString(dateLocale, {
      weekday: "short",
      day: "2-digit",
      month: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
    return { tone: "snoozed", text: t("snoozedUntil", { until }) };
  }
  if (!job.next_run_at) return { tone: "active", text: t("stateActive") };
  return { tone: "active", text: `${t("stateActive")} · ${t("stateNextRun", { when: ago(job.next_run_at) })}` };
}
