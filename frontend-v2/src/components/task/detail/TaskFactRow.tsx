"use client";

/**
 * Compact facts row under the state card. The layout follows the width of
 * the CONTAINER, not the window (the same body runs in the Home modal
 * ~672 px, the /tasks split ~800 px and on the phone 390 px):
 *
 *   < 560 px   3-column grid (3×2 on the phone)
 *   ≥ 560 px   one wrapping line of [LABEL value] pairs
 *
 * The ≥1000 px side rail is wave 3b. The parent must be an `@container`.
 */

import { useLocale, useTranslations } from "next-intl";
import { C, STATUS_TEXT } from "@/lib/colors";
import { formatDuration, formatUsd, secondsBetween } from "@/lib/taskDetail/format";
import type { Agent, RunRecord, Task } from "@/lib/types";
import { AgentMonogram } from "./AgentMonogram";
import { PrChip } from "./PrChip";

const TERMINAL = new Set(["done", "failed", "aborted"]);

function Fact({
  label,
  children,
  testId,
  className = "",
}: {
  label: string;
  children: React.ReactNode;
  testId?: string;
  className?: string;
}) {
  return (
    <div
      className={`min-w-0 ${className} px-2.5 py-2 @min-[560px]:px-0 @min-[560px]:py-0 flex flex-col @min-[560px]:flex-row @min-[560px]:items-center gap-0.5 @min-[560px]:gap-1.5`}
      style={{ background: "var(--fact-bg)" }}
      data-testid={testId}
    >
      <span className="label-sys label-sys--dim shrink-0">{label}</span>
      <span className="text-xs min-w-0 truncate flex items-center gap-1" style={{ color: C.textPrimary }}>
        {children}
      </span>
    </div>
  );
}

export function TaskFactRow({
  task,
  agent,
  statusControl,
  runRecord,
  checklist,
  headFact = null,
}: {
  task: Task;
  agent: Agent | undefined;
  /** The status dropdown (StatusMenu) — the status menu stays. */
  statusControl: React.ReactNode;
  runRecord: RunRecord | null | undefined;
  checklist: { done: number; total: number };
  /** "omp · GLM local" when the task has a head run (head launcher §8.2). */
  headFact?: string | null;
}) {
  const t = useTranslations("tasks");
  const tHeads = useTranslations("heads");
  const locale = useLocale();

  // inbox = not started for the current run, even if an earlier run was
  // dispatched (recurring jobs go back to inbox).
  const notStarted = task.status === "inbox";
  const timeSeconds = TERMINAL.has(task.status)
    ? (runRecord?.zeiten.dauer_sekunden ?? secondsBetween(task.created_at, task.completed_at))
    : secondsBetween(task.created_at, null);
  const time = notStarted ? t("detail.notStarted") : (formatDuration(timeSeconds, locale) ?? "—");

  // Never "$0.00" when nothing is tracked: Claude work has no usage rows at
  // all, so zero would be a false statement (concept §5.2).
  const kosten = runRecord?.kosten;
  const billed = kosten && kosten.gesamt_usd > 0 ? formatUsd(kosten.gesamt_usd) : null;
  const cost = kosten ? (
    // Grid (phone): amount and hint stacked, nothing cut off. Row: one line.
    <span className="flex flex-col @min-[560px]:flex-row @min-[560px]:items-center @min-[560px]:gap-1 min-w-0">
      {billed && <span>{billed}</span>}
      {kosten.hinweis && (
        <span className="whitespace-normal @min-[560px]:whitespace-nowrap" style={{ color: C.textMuted }}>
          {billed && <span className="hidden @min-[560px]:inline">· </span>}
          {t("detail.claudeNotTracked")}
        </span>
      )}
      {!billed && !kosten.hinweis && <span>{formatUsd(0)}</span>}
    </span>
  ) : (
    "—"
  );

  const showPriority = task.priority === "high" || task.priority === "critical";

  return (
    <div
      data-testid="task-fact-row"
      className="grid grid-cols-3 gap-px rounded-lg overflow-hidden @min-[560px]:flex @min-[560px]:flex-wrap @min-[560px]:gap-x-4 @min-[560px]:gap-y-1.5 @min-[560px]:rounded-none @min-[560px]:overflow-visible [--fact-bg:var(--color-bg-surface)] @min-[560px]:[--fact-bg:transparent] bg-[var(--color-border)] @min-[560px]:bg-transparent"
    >
      <Fact label={t("detail.factStatus")} testId="fact-status">{statusControl}</Fact>
      <Fact label={t("detail.factAgent")} testId="fact-agent">
        {agent ? (
          <>
            <AgentMonogram name={agent.name} />
            <span className="truncate">{agent.name}</span>
          </>
        ) : (
          <span style={{ color: C.textMuted }}>{t("detail.unassigned")}</span>
        )}
      </Fact>
      <Fact label={t("detail.factTime")} testId="fact-time">
        <span className="font-mono">{time}</span>
      </Fact>
      <Fact label={t("detail.factPr")} testId="fact-pr">
        {task.pr_url ? <PrChip url={task.pr_url} number={task.pr_number} /> : "—"}
      </Fact>
      <Fact label={t("detail.factPlan")} testId="fact-plan">
        <span className="font-mono">{checklist.total > 0 ? `✓ ${checklist.done}/${checklist.total}` : "—"}</span>
      </Fact>
      <Fact label={t("detail.factCost")} testId="fact-cost">{cost}</Fact>
      {headFact && (
        <Fact label={tHeads("runs.fact")} testId="fact-head" className="col-span-3">
          <span className="truncate">{headFact}</span>
        </Fact>
      )}
      {showPriority && (
        <Fact label={t("detail.factPriority")} testId="fact-priority" className="col-span-3">
          <span style={{ color: task.priority === "critical" ? STATUS_TEXT.error : STATUS_TEXT.warning }}>
            {task.priority === "critical" ? t("priorityCritical") : t("priorityHigh")}
          </span>
        </Fact>
      )}
    </div>
  );
}
