"use client";

/**
 * Bench W1 P1 (package D) — "Testen" on the Runtimes page.
 *
 * Everything the card says comes from GET /bench/runtime-summary
 * (lib/benchLinks.BenchRuntimeSummary), nothing is guessed here:
 *   - `primary` → accent button (only the most recently started live card, K11)
 *   - `reason` model_fresh → "Testen ab 22:27" (the test waits for the 15-min
 *     rule), box_busy → "Testen (wartet)"; both still start a test
 *   - `testable:false` → a disabled button + the blocker in words
 *   - `running_test_id` → "Test läuft", opens that test
 *   - `last_test` → "Letzter Test: 22.10. · Pagoda Garden", opens that result
 * A box without a model cannot be tested: it says so (mockup 01 "offline").
 */

import Link from "next/link";
import { useLocale, useTranslations } from "next-intl";
import { FlaskConical } from "lucide-react";
import { C } from "@/lib/colors";
import { benchEnabled, benchResultHref, benchTestHref, type BenchRuntimeSummary } from "@/lib/benchLinks";

const BASE =
  "inline-flex items-center justify-center gap-2 h-11 rounded-md text-sm whitespace-nowrap transition-colors";

function clock(iso: unknown, locale: string): string | null {
  if (typeof iso !== "string") return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  return new Intl.DateTimeFormat(locale, { hour: "2-digit", minute: "2-digit" }).format(d);
}

function day(iso: string, locale: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return new Intl.DateTimeFormat(locale, { day: "2-digit", month: "2-digit" }).format(d);
}

/** The button on a live card. `primary` comes from the summary row (K11). */
export function BenchTestAction({ summary, className = "" }: { summary: BenchRuntimeSummary; className?: string }) {
  const t = useTranslations("benchHome.runtimes");
  const locale = useLocale();
  const primary = summary.primary;
  const style = primary
    ? { background: C.accent, color: C.onAccent, border: `1px solid ${C.accent}` }
    : { color: C.textPrimary, border: `1px solid ${C.borderActive}` };
  const tone = primary
    ? "px-4 font-semibold hover:brightness-110"
    : "px-3 font-medium hover:bg-[var(--color-bg-hover)]";

  if (!summary.testable && !summary.running_test_id && !summary.planned_test) {
    return (
      <button
        type="button"
        aria-disabled="true"
        aria-describedby={`bench-note-${summary.runtime_id}`}
        data-testid="bench-test"
        data-primary="false"
        onClick={(e) => e.preventDefault()}
        className={`${BASE} px-3 font-medium cursor-not-allowed ${className}`}
        style={{ color: C.textMuted, border: `1px solid ${C.borderSubtle}` }}
      >
        <FlaskConical size={16} aria-hidden />
        {t("test")}
      </button>
    );
  }

  let href = benchTestHref({ runtimeId: summary.runtime_id, trigger: "runtimes" });
  let label = t("test");
  const planned = !summary.running_test_id ? summary.planned_test ?? null : null;
  if (planned) {
    // The auto test after the switch is already planned: open it — one tap
    // here must never plan a second test on the same model (review S3).
    href = benchResultHref(planned.id);
    const at = clock(planned.not_before, locale);
    label = at ? t("testPlannedAt", { time: at }) : t("testPlanned");
  } else if (summary.running_test_id) {
    href = benchResultHref(summary.running_test_id);
    label = t("testRunning");
  } else if (summary.reason?.code === "model_fresh") {
    const at = clock(summary.reason.params?.until, locale);
    if (at) label = t("testFrom", { time: at });
  } else if (summary.reason?.code === "box_busy") {
    label = t("testWaits");
  }

  return (
    <Link
      href={href}
      data-testid="bench-test"
      data-primary={primary ? "true" : "false"}
      className={`${BASE} ${tone} ${className}`}
      style={style}
    >
      <FlaskConical size={16} aria-hidden />
      <span className="truncate">{label}</span>
    </Link>
  );
}

/** Why the button reads as it does + the last test. Nothing when there is nothing to say (K3). */
export function BenchTestNotes({ summary }: { summary: BenchRuntimeSummary }) {
  const t = useTranslations("benchHome.runtimes");
  const tShared = useTranslations("benchShared");
  const locale = useLocale();

  let note: string | null = null;
  if (!summary.running_test_id && !summary.planned_test) {
    const code = summary.reason?.code ?? null;
    if (!summary.testable) {
      note = code && tShared.has(`blocker.${code}`) ? tShared(`blocker.${code}`) : t("blocked");
    } else if (code === "model_fresh") {
      note = t("freshNote");
    } else if (code === "box_busy") {
      note = t("busyNote");
    } else if (code && tShared.has(`blocker.${code}`)) {
      note = tShared(`blocker.${code}`);
    }
  }
  const last = summary.last_test;
  if (!note && !last) return null;

  return (
    <div className="flex flex-col text-xs" data-testid="bench-notes">
      {note && (
        <span id={`bench-note-${summary.runtime_id}`} data-testid="bench-note" style={{ color: C.textSecondary }}>
          {note}
        </span>
      )}
      {last && (
        <Link
          href={benchResultHref(last.id)}
          data-testid="bench-last-test"
          className="self-start inline-flex items-center min-h-11 hover:underline underline-offset-4"
          style={{ color: C.textMuted }}
        >
          {t("lastTest", { date: day(last.created_at, locale), title: last.title })}
        </Link>
      )}
    </div>
  );
}

/** A box with no model loaded (asleep or free): testing needs a running
 *  model. The button sits in the card's action row, the reason on its own
 *  line under it (`NoModelTestHint`, mockup 01 "offline"). */
export function NoModelTestButton() {
  const t = useTranslations("benchHome.runtimes");
  if (!benchEnabled) return null;
  return (
    <button
      type="button"
      aria-disabled="true"
      aria-label={`${t("test")} — ${t("noModel")}`}
      data-testid="bench-test-no-model"
      onClick={(e) => e.preventDefault()}
      className={`${BASE} px-3 font-medium cursor-not-allowed`}
      style={{ color: C.textMuted }}
    >
      <FlaskConical size={16} aria-hidden />
      {t("test")}
    </button>
  );
}

export function NoModelTestHint() {
  const t = useTranslations("benchHome.runtimes");
  if (!benchEnabled) return null;
  return (
    <span className="basis-full text-right text-xs" style={{ color: C.textMuted }}>
      {t("noModel")}
    </span>
  );
}

/** Cloud tab row (W1.12): a quiet text button where the summary says testable. */
export function CloudTestLink({ summary }: { summary: BenchRuntimeSummary | undefined }) {
  const t = useTranslations("benchHome.runtimes");
  if (!summary || !summary.testable) return null;
  const href = summary.running_test_id
    ? benchResultHref(summary.running_test_id)
    : benchTestHref({ runtimeId: summary.runtime_id, trigger: "runtimes" });
  return (
    <Link
      href={href}
      onClick={(e) => e.stopPropagation()}
      data-testid="bench-test-cloud"
      className="inline-flex items-center gap-2 min-h-11 px-2 -mr-2 rounded-md text-sm font-medium shrink-0 hover:bg-[var(--color-bg-hover)]"
      style={{ color: C.textPrimary }}
    >
      <FlaskConical size={15} aria-hidden />
      {summary.running_test_id ? t("testRunning") : t("test")}
    </Link>
  );
}
