"use client";

/**
 * Insights → "What got done": tasks finished and failed in the analysis
 * window, one row per agent that did something, the heads' runs of the same
 * window, then anomalies and failure reasons as plain sentences — only when
 * there are any (K3). One status colour at most (K6): the anomaly dot. Anomaly sentences are built here from the structured
 * fields; the backend's own description is not translated.
 *
 *   14 tasks done   2 failed
 *   Heads        9 runs · 7 passed · 2 failed
 *   Hermes       4 done
 *   Rex          3 done · 1 failed
 *   ● 1 task took far longer than usual.
 */

import { useLocale, useTranslations } from "next-intl";
import { C } from "@/lib/colors";
import type { IntelligenceInsights } from "@/lib/types";
import type { HeadRun } from "@/lib/heads";

export function headsInWindow(runs: HeadRun[], days: number, now = Date.now()) {
  const since = now - days * 86_400_000;
  const recent = runs.filter((r) => r.created_at && Date.parse(r.created_at) >= since);
  return {
    runs: recent.length,
    passed: recent.filter((r) => r.state === "passed").length,
    failed: recent.filter((r) => r.state === "failed").length,
  };
}

export function DoneSummary({
  insights,
  windowDays,
  heads,
}: {
  insights: IntelligenceInsights | undefined;
  windowDays: number;
  heads: HeadRun[] | null;
}) {
  const t = useTranslations("insights.done");
  const locale = useLocale();
  const agents = (insights?.agent_performance ?? []).filter((a) => a.done > 0 || a.failed > 0)
    .sort((a, b) => b.done + b.failed - (a.done + a.failed));
  const done = agents.reduce((s, a) => s + a.done, 0);
  const failed = agents.reduce((s, a) => s + a.failed, 0);
  const headStats = heads ? headsInWindow(heads, windowDays) : null;
  const patterns = Object.entries(insights?.failure_patterns?.patterns ?? {}).sort((a, b) => b[1] - a[1]);
  const pct = (rate: number) => new Intl.NumberFormat(locale, { style: "percent", maximumFractionDigits: 0 }).format(rate / 100);

  const notes = (insights?.anomalies ?? []).map((a) => {
    const text =
      a.type === "slow_tasks"
        ? t("slowTasks", { count: insights?.task_durations?.outliers?.length ?? 1 })
        : a.type === "low_success_rate" && a.agent_name
          ? t("lowSuccess", {
              agent: a.agent_name,
              rate: pct(insights?.agent_performance?.find((p) => p.name === a.agent_name)?.success_rate ?? 0),
            })
          : a.type === "high_failure_rate"
            ? t("highFailure", { count: insights?.failure_patterns?.total ?? 0, pattern: patterns[0]?.[0] ?? "unknown" })
            : a.description;
    return { key: `${a.type}-${a.agent_name ?? ""}`, text, warning: a.severity === "warning" };
  });

  return (
    <section data-region="done" aria-labelledby="done-heading" className="min-w-0">
      <div className="flex items-baseline justify-between gap-3">
        <h2 id="done-heading" className="text-base font-semibold" style={{ color: C.textPrimary }}>
          {t("title")}
        </h2>
        <span className="text-xs shrink-0" style={{ color: C.textMuted }}>{t("period", { days: windowDays })}</span>
      </div>

      {!insights?.analyzed_at ? (
        <p className="mt-3 text-sm" style={{ color: C.textSecondary }}>{t("pending")}</p>
      ) : (
        <>
          <p className="mt-3 flex flex-wrap items-baseline gap-x-6 gap-y-1" data-testid="done-totals">
            <span className="flex items-baseline gap-2">
              <span className="display text-xl font-medium tabular-nums" style={{ color: C.textPrimary }}>{done}</span>
              <span className="text-sm" style={{ color: C.textSecondary }}>{t("tasksDone")}</span>
            </span>
            <span className="flex items-baseline gap-2">
              <span className="display text-xl font-medium tabular-nums" style={{ color: C.textPrimary }}>
                {failed}
              </span>
              <span className="text-sm" style={{ color: C.textSecondary }}>{t("tasksFailed")}</span>
            </span>
          </p>

          <ul className="mt-3">
            {headStats && headStats.runs > 0 && (
              <Row name={t("heads")} value={t("headsRuns", headStats)} testId="done-heads" />
            )}
            {agents.map((a) => (
              <Row
                key={a.agent_id}
                name={a.name}
                value={a.failed > 0 ? t("agentDoneFailed", { done: a.done, failed: a.failed }) : t("agentDone", { done: a.done })}
                testId={`done-agent-${a.name}`}
              />
            ))}
          </ul>
          {agents.length === 0 && (!headStats || headStats.runs === 0) && (
            <p className="mt-1 text-sm" style={{ color: C.textSecondary }}>{t("nothing")}</p>
          )}

          {patterns.length > 0 && (
            <p className="mt-3 text-sm" style={{ color: C.textSecondary }} data-testid="done-patterns">
              {t("patterns")}: {patterns.map(([p, n]) => t("pattern", { pattern: p, count: n })).join(" · ")}
            </p>
          )}

          {notes.length > 0 && (
            <ul className="mt-3" data-testid="done-notes">
              {notes.map((n) => (
                <li key={n.key} className="flex items-start gap-3 py-2 text-sm" style={{ color: C.textSecondary }}>
                  <span
                    aria-hidden
                    className="mt-2 size-2 shrink-0 rounded-full"
                    style={{ background: n.warning ? C.warning : C.info }}
                  />
                  <span>{n.text}</span>
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </section>
  );
}

function Row({ name, value, testId }: { name: string; value: string; testId: string }) {
  return (
    <li
      className="flex items-center justify-between gap-3 min-h-11"
      style={{ borderBottom: `1px solid ${C.borderSubtle}` }}
      data-testid={testId}
    >
      <span className="text-sm truncate" style={{ color: C.textPrimary }}>{name}</span>
      <span className="text-sm tabular-nums shrink-0" style={{ color: C.textSecondary }}>{value}</span>
    </li>
  );
}
