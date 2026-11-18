"use client";

/**
 * HelperLine — one quiet line for every helper (subagent) the agent started
 * in one working stretch: "4 helpers · 3 working   12 min ›". Tap to see each
 * helper with its state and duration; a Claude helper whose own transcript is
 * known opens it there (AgentCard).
 *
 * Why one line and not a card per helper: a stretch that fans out to five
 * helpers used to stack five bordered cards, each claiming "done" the second
 * it launched. The line says the one thing the operator wants at a glance —
 * how many are still working — and stays out of the way otherwise.
 *
 * State comes only from observations (`helpers.ts`): the launch state the
 * backend read from the harness, then each helper's own report.
 */
import { useEffect, useMemo, useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import { ChevronRight } from "lucide-react";
import { C } from "@/lib/colors";
import type { SubagentRun, ToolEvent } from "@/lib/chatTypes";
import { formatDuration } from "@/lib/taskDetail/format";
import { AgentCard, HELPER_DOT } from "./AgentCard";
import {
  collectHelpers,
  groupElapsedSeconds,
  summarizeHelpers,
  type HelperReports,
  type HelperState,
  type HelperSummary,
} from "./helpers";

/** "3 working" while any works (plus failures, they matter); once all are
 *  finished "done", or "3 done, 1 failed". */
export function helperStateLabel(
  s: HelperSummary,
  t: (key: string, values?: Record<string, string | number | Date>) => string,
): string {
  const parts: string[] = [];
  if (s.working > 0) {
    parts.push(t("helperLine.working", { count: s.working }));
    if (s.failed > 0) parts.push(t("helperLine.failed", { count: s.failed }));
  } else if (s.failed === 0) {
    parts.push(t("helperLine.allDone"));
  } else {
    if (s.done > 0) parts.push(t("helperLine.done", { count: s.done }));
    parts.push(t("helperLine.failed", { count: s.failed }));
  }
  return parts.join(", ");
}

function lineState(s: HelperSummary): HelperState {
  if (s.working > 0) return "working";
  return s.failed > 0 ? "failed" : "done";
}

/** A clock that ticks only while something is running — minute precision is
 *  all the line shows (K17), so every 15 s is plenty. */
function useTick(active: boolean): Date {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    if (!active) return;
    const id = setInterval(() => setNow(new Date()), 15_000);
    return () => clearInterval(id);
  }, [active]);
  return now;
}

export function HelperLine({
  events,
  reports,
  runs,
  agentId,
}: {
  events: ToolEvent[];
  reports: HelperReports;
  /** Claude subagent transcripts, by the call's toolUseId (`matchRuns`). */
  runs: Map<string, SubagentRun>;
  agentId: string;
}) {
  const t = useTranslations("sessions");
  const locale = useLocale();
  const [expanded, setExpanded] = useState(false);

  const helpers = useMemo(() => collectHelpers(events, reports), [events, reports]);
  const summary = summarizeHelpers(helpers);
  const now = useTick(summary.working > 0);
  if (helpers.length === 0) return null;

  const seconds = groupElapsedSeconds(helpers, now);
  const duration = seconds != null && seconds >= 60 ? formatDuration(seconds, locale) : null;

  return (
    <div className="w-full px-4 md:px-5 py-1" data-testid="helper-line">
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
        aria-label={expanded ? t("helperLine.collapse") : t("helperLine.expand")}
        className="flex w-full items-center gap-2 px-2.5 text-left min-h-[44px] md:min-h-[34px] cursor-pointer bg-transparent border-0"
      >
        <span
          aria-hidden="true"
          className="shrink-0 w-1.5 h-1.5 rounded-full"
          style={{ background: HELPER_DOT[lineState(summary)] }}
        />
        <span className="min-w-0 truncate text-sm" style={{ color: C.textSecondary }} data-testid="helper-line-label">
          {t("helperLine.count", { count: summary.total })}
          <span style={{ color: C.textMuted }}> · {helperStateLabel(summary, t)}</span>
        </span>
        <span className="ml-auto flex items-center gap-2 shrink-0">
          {duration && (
            <span className="text-xs tabular-nums" style={{ color: C.textMuted }} data-testid="helper-line-duration">
              {duration}
            </span>
          )}
          <ChevronRight
            size={13}
            className="shrink-0 transition-transform duration-150"
            style={{ color: C.textMuted, transform: expanded ? "rotate(90deg)" : undefined }}
            aria-hidden="true"
          />
        </span>
      </button>

      {expanded && (
        // The same hairline rail as an opened tool group: the rows belong to
        // the line above, without a nested card (Flach-Regel, K8).
        <div className="mt-1 ml-3.5 pl-1" style={{ borderLeft: `1px solid ${C.border}` }} data-testid="helper-list">
          {helpers.map((h) => (
            <AgentCard
              key={h.key}
              ev={h.spawn}
              helper={h}
              now={now}
              // A run belongs to a call with ONE helper only (Claude Code).
              run={h.spawn.helpers && h.spawn.helpers.length > 1 ? undefined : runs.get(h.spawn.toolUseId ?? "")}
              agentId={agentId}
            />
          ))}
        </div>
      )}
    </div>
  );
}
