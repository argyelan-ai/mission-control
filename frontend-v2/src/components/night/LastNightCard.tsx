"use client";

/**
 * Home → "Last night" (ROADMAP E2). MC is the operator's channel: the morning
 * report and the blocked notices of the night shift land here, on the phone
 * as on the desktop. Slack / Telegram only get a copy when the operator
 * switched that on (Settings → Night shift, default off).
 *
 *   ☾ LAST NIGHT · THU 23 SEP                                         [×]
 *   ● 1 passed  ● 1 failed  ● 1 needs you  ● 1 blocked
 *   ? Remove old endpoint                                        [Answer]
 *     needs you · "Two callers still use it. Remove anyway?"
 *   ■ Docs pass — not started · the model is not running
 *   ✕ Upload worker — failed · The harness ended with exit code 1.
 *   ✓ Fix flaky retry test — passed · PR #9 ↗
 *
 * During the night (no report yet) the same card shows "Night shift · now"
 * with the heads that are blocked right now (a question, or silent 15 min).
 *
 * One line per job; the title opens the task. "Answer" = restart on the same
 * pair with the answer (the head card's "Answer & continue"). The card stays
 * until dismissed or until the next night starts (the backend decides).
 * Renders nothing while heads are off or when nothing ran.
 */

import { useState } from "react";
import Link from "next/link";
import { useLocale, useTranslations } from "next-intl";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ExternalLink, Moon, Send, X } from "lucide-react";
import { api } from "@/lib/api";
import { notify } from "@/lib/notify";
import { C, STATUS_TEXT } from "@/lib/colors";
import { failReasonKey, headErrorKey, prNumberFromUrl, type HeadRun } from "@/lib/heads";
import {
  NIGHT_CATEGORIES,
  canAnswer,
  hasLastNight,
  lastNightRows,
  nightReasonKey,
  reportCounts,
  type LastNight,
  type LastNightEntry,
  type NightCategory,
  type NightNotice,
} from "@/lib/nightShift";
import { useHeadsEnabled } from "@/components/heads/useHeadsEnabled";

export const LAST_NIGHT_KEY = ["nightShift", "lastNight"] as const;

// Colour = status (DESIGN.md). Waiting on the operator = the brightest tone.
const TONE: Record<NightCategory, string> = {
  passed: C.online,
  failed: C.error,
  needs_you: C.accent,
  blocked: C.warning,
  running: C.info,
};
const TEXT_TONE: Record<NightCategory, string> = {
  passed: STATUS_TEXT.online,
  failed: STATUS_TEXT.error,
  needs_you: C.accent,
  blocked: STATUS_TEXT.warning,
  running: STATUS_TEXT.info,
};
const GLYPH: Record<NightCategory, string> = {
  passed: "✓",
  failed: "✕",
  needs_you: "?",
  blocked: "■",
  running: "●",
};

const primaryBtn =
  "inline-flex items-center justify-center gap-1.5 px-3.5 min-h-[44px] sm:min-h-[36px] rounded-md text-xs font-semibold cursor-pointer transition-colors hover:bg-[var(--color-accent-light)] disabled:opacity-40 disabled:cursor-not-allowed";
const ghostBtn =
  "inline-flex items-center justify-center gap-1.5 px-3 min-h-[44px] sm:min-h-[36px] rounded-md text-xs font-medium cursor-pointer transition-colors hover:bg-[var(--color-bg-hover)] disabled:opacity-40 disabled:cursor-not-allowed";

export function LastNightCard() {
  const headsEnabled = useHeadsEnabled();
  const q = useQuery<LastNight>({
    queryKey: LAST_NIGHT_KEY,
    queryFn: () => api.nightShift.lastNight(),
    enabled: headsEnabled === true,
    retry: false,
    refetchInterval: 60_000,
  });
  if (headsEnabled !== true || !q.data) return null;
  return <LastNightCardView data={q.data} />;
}

export function LastNightCardView({ data }: { data: LastNight }) {
  const t = useTranslations("nightShift");
  const locale = useLocale();
  const qc = useQueryClient();
  const [hidden, setHidden] = useState<string | null>(null);

  const dismiss = useMutation({
    mutationFn: (night: string) => api.nightShift.dismissLastNight(night),
    onMutate: (night) => setHidden(night),
    onSuccess: () => qc.invalidateQueries({ queryKey: LAST_NIGHT_KEY }),
    onError: () => {
      setHidden(null);
      notify.error(t("errors.unknown"));
    },
  });

  const report = data.report && data.report.night !== hidden ? data.report : null;
  const view: LastNight = { report, notices: data.notices };
  if (!hasLastNight(view)) return null;

  const counts = reportCounts(report);
  const rows = lastNightRows(report);
  const nightLabel = report ? formatNight(report.night, locale) : null;

  return (
    <section
      className="rounded-xl corner-ticks p-3 sm:p-4"
      style={{ background: C.bgSurface, border: `1px solid ${C.border}` }}
      aria-labelledby="last-night-heading"
      data-testid="last-night-card"
    >
      <div className="flex items-center gap-2">
        <Moon size={13} aria-hidden style={{ color: C.textMuted }} className="shrink-0" />
        <h2 id="last-night-heading" className="label-sys flex-1 min-w-0 truncate">
          {report ? `${t("card.title")} · ${nightLabel}` : t("card.titleNow")}
        </h2>
        {report && (
          <button
            type="button"
            onClick={() => dismiss.mutate(report.night)}
            aria-label={t("card.dismissAria")}
            title={t("card.dismiss")}
            data-testid="last-night-dismiss"
            className="shrink-0 -my-2 -mr-2 inline-flex items-center justify-center min-w-[44px] min-h-[44px] rounded-md cursor-pointer transition-colors hover:bg-[var(--color-bg-hover)]"
            style={{ color: C.textMuted }}
          >
            <X size={14} aria-hidden />
          </button>
        )}
      </div>

      {report && (
        <ul className="mt-2 flex flex-wrap gap-x-4 gap-y-1" data-testid="last-night-counts">
          {NIGHT_CATEGORIES.filter((c) => c !== "running" || counts.running > 0).map((c) => (
            <li key={c} className="inline-flex items-center gap-1.5 text-xs" style={{ color: counts[c] > 0 ? TEXT_TONE[c] : C.textMuted }}>
              <span
                aria-hidden
                className="inline-block w-1.5 h-1.5 rounded-full"
                style={{ background: counts[c] > 0 ? TONE[c] : C.textDim }}
              />
              <span className="font-mono tabular-nums">{t("card.count", { count: counts[c], label: t(`category.${c}`) })}</span>
            </li>
          ))}
        </ul>
      )}

      {rows.length > 0 && (
        <ul className="mt-3 divide-y" style={{ borderColor: C.border }}>
          {rows.map((e) => (
            <ReportRow key={e.task_id} entry={e} />
          ))}
        </ul>
      )}

      {data.notices.length > 0 && (
        <div className={rows.length > 0 ? "mt-3 pt-3" : "mt-2"} style={rows.length > 0 ? { borderTop: `1px solid ${C.border}` } : undefined}>
          {rows.length > 0 && <h3 className="label-sys mb-1">{t("card.nowTitle")}</h3>}
          <ul className="divide-y" style={{ borderColor: C.border }}>
            {data.notices.map((n) => (
              <NoticeRow key={n.task_id} notice={n} />
            ))}
          </ul>
        </div>
      )}

      {report?.delivered && (
        <p className="mt-2 text-[11px]" style={{ color: C.textMuted }}>{t("card.alsoSent")}</p>
      )}
    </section>
  );
}

function formatNight(night: string, locale: string): string {
  const d = new Date(`${night}T12:00:00`);
  if (Number.isNaN(d.getTime())) return night;
  return d.toLocaleDateString(locale, { weekday: "short", day: "numeric", month: "short" });
}

/** One job: glyph · title (opens the task) · one detail line · optional Answer. */
function JobLine({
  testId,
  taskId,
  title,
  category,
  detail,
  question,
  run,
  action,
}: {
  testId: string;
  taskId: string;
  title: string;
  category: NightCategory;
  detail: React.ReactNode;
  question?: string | null;
  run?: HeadRun | null;
  action?: React.ReactNode;
}) {
  const t = useTranslations("nightShift");
  const [answering, setAnswering] = useState(false);
  const answerable = canAnswer(run);
  return (
    <li data-testid={testId} className="py-1.5 first:pt-0 last:pb-0" style={{ borderColor: C.border }}>
      <div className="flex items-start gap-2.5">
        <span
          aria-hidden
          className="shrink-0 w-4 text-center font-mono text-xs leading-[44px] sm:leading-[36px]"
          style={{ color: TONE[category] }}
        >
          {GLYPH[category]}
        </span>
        <div className="flex-1 min-w-0">
          <Link
            href={`/tasks?task=${encodeURIComponent(taskId)}`}
            aria-label={t("card.openTaskAria", { title })}
            className="flex items-center min-h-[44px] sm:min-h-[36px] text-sm font-medium hover:underline"
            style={{ color: C.textPrimary }}
          >
            <span className="truncate">{title}</span>
          </Link>
          <p className="-mt-1.5 text-xs break-words" style={{ color: C.textSecondary }}>
            <span style={{ color: TEXT_TONE[category] }}>{t(`category.${category}`)}</span>
            {detail ? <> · {detail}</> : null}
          </p>
          {question && (
            <p className="mt-1 text-xs whitespace-pre-wrap break-words" style={{ color: C.textPrimary }}>
              “{question}”
            </p>
          )}
        </div>
        {action}
        {answerable && !answering && (
          <button
            type="button"
            onClick={() => setAnswering(true)}
            aria-label={t("card.answerAria", { title })}
            data-testid={`answer-${testId}`}
            className={`${primaryBtn} shrink-0`}
            style={{ background: C.accent, color: C.onAccent }}
          >
            {t("card.answer")}
          </button>
        )}
      </div>
      {answerable && answering && <AnswerForm run={run} onCancel={() => setAnswering(false)} />}
    </li>
  );
}

/** "Answer & continue": restart on the SAME pair, same branch, with the answer. */
function AnswerForm({ run, onCancel }: { run: HeadRun; onCancel: () => void }) {
  const t = useTranslations("nightShift");
  const tHeads = useTranslations("heads");
  const qc = useQueryClient();
  const [answer, setAnswer] = useState("");
  const id = `last-night-answer-${run.run_id}`;
  const send = useMutation({
    mutationFn: () =>
      api.heads.restart(run.run_id, {
        harness: run.harness ?? "",
        runtime_slug: run.runtime_slug ?? "",
        mode: "continue",
        answer: answer.trim(),
      }),
    onSuccess: () => {
      setAnswer("");
      onCancel();
      notify.success(tHeads("restart.done"));
      qc.invalidateQueries({ queryKey: ["nightShift"] });
      qc.invalidateQueries({ queryKey: ["heads"] });
      qc.invalidateQueries({ queryKey: ["tasks"] });
    },
    onError: (err) => notify.error(tHeads(headErrorKey(err))),
  });
  return (
    <div className="mt-2 sm:pl-6 space-y-2">
      <label htmlFor={id} className="label-sys block">{tHeads("card.answerLabel")}</label>
      <textarea
        id={id}
        value={answer}
        onChange={(e) => setAnswer(e.target.value)}
        rows={3}
        autoFocus
        placeholder={tHeads("card.answerPlaceholder")}
        className="w-full rounded-md px-3 py-2 text-base sm:text-sm outline-none resize-y"
        style={{ background: C.bgDeep, border: `1px solid ${C.border}`, color: C.textPrimary }}
      />
      <div className="flex flex-col-reverse sm:flex-row sm:justify-end gap-2">
        <button type="button" onClick={onCancel} className={ghostBtn} style={{ color: C.textSecondary, border: `1px solid ${C.borderActive}` }}>
          {t("card.cancel")}
        </button>
        <button
          type="button"
          onClick={() => send.mutate()}
          disabled={!answer.trim() || send.isPending}
          className={primaryBtn}
          style={{ background: C.accent, color: C.onAccent }}
        >
          <Send size={12} aria-hidden />
          {tHeads("card.answerContinue")}
        </button>
      </div>
    </div>
  );
}

function ReportRow({ entry: e }: { entry: LastNightEntry }) {
  const t = useTranslations("nightShift");
  const tHeads = useTranslations("heads");
  let detail: React.ReactNode = null;
  let question: string | null = null;
  let action: React.ReactNode = null;
  if (e.category === "passed" && e.pr_url) {
    const n = prNumberFromUrl(e.pr_url);
    // The row's second action sits where "Answer" sits: a 44 px target on phones.
    action = (
      <a
        href={e.pr_url}
        target="_blank"
        rel="noopener noreferrer"
        className={`${ghostBtn} shrink-0 font-mono`}
        style={{ color: C.textPrimary, border: `1px solid ${C.borderActive}` }}
      >
        {n != null ? t("card.pr", { number: n }) : t("card.prNoNumber")}
        <ExternalLink size={11} aria-hidden />
      </a>
    );
  } else if (e.category === "failed") {
    const fr = failReasonKey(e.reason);
    detail = tHeads(fr.key, fr.values);
  } else if (e.category === "blocked") {
    if (!e.started) detail = `${t("card.notStarted")} · ${t(nightReasonKey(e.reason))}`;
    else if (e.blocked === "silent" && e.silent_s) {
      detail = t("card.silentFor", { minutes: Math.max(1, Math.round(e.silent_s / 60)) });
    }
  } else if (e.category === "needs_you") {
    if (e.run && e.run.state === "needs_you") question = e.run.question;
    else if (e.run) detail = t("card.answeredElsewhere");
  }
  return (
    <JobLine
      testId={`last-night-row-${e.task_id}`}
      taskId={e.task_id}
      title={e.title}
      category={e.category}
      detail={detail}
      question={question}
      run={e.run}
      action={action}
    />
  );
}

function NoticeRow({ notice: n }: { notice: NightNotice }) {
  const t = useTranslations("nightShift");
  const needsYou = n.kind === "needs_you";
  const minutes = Math.max(1, Math.round((n.silent_s ?? 0) / 60));
  return (
    <JobLine
      testId={`last-night-notice-${n.task_id}`}
      taskId={n.task_id}
      title={n.title}
      category={needsYou ? "needs_you" : "blocked"}
      detail={needsYou ? t("card.waitsForYou") : t("card.silentFor", { minutes })}
      question={needsYou ? n.run.question : null}
      run={n.run}
    />
  );
}
