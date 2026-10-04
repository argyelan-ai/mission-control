"use client";

/**
 * ToolGroup — one tappable summary row standing in for a run of consecutive
 * tool/thinking events, the way the Claude app collapses an agent's working
 * stretch into "3 commands executed, 2 tools used ›".
 *
 * Why: a transcript's tool rows outnumber its prose by an order of magnitude.
 * Rendering each one is truthful but unreadable — the reader loses the
 * conversation inside the machinery. The group keeps every row (tap to open,
 * "Verbose" opens them all) while making the DEFAULT reading experience the
 * conversation itself.
 *
 * Grouping/boundary logic lives in ChatView (`buildTimelineItems`) — this
 * component only renders a run it is handed. `summarizeActivity` is exported
 * because the counting is the part with the interesting edge cases (thinking
 * vs. tool runs, error aggregation) and deserves its own tests, independent
 * of the i18n wording `toolGroupLabel` builds on top of it (review finding on
 * PR #756 round 2: the label used to be hardcoded German text inside a
 * component the rest of the English UI never mixes languages in).
 */
import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { AlertTriangle, Brain, ChevronRight, Terminal, Wrench } from "lucide-react";
import { C, STATUS_TEXT } from "@/lib/colors";
import type { ThinkingEvent, ToolEvent } from "@/lib/chatTypes";
import { ToolRow } from "./ToolRow";
import { ThinkingRow } from "./ThinkingRow";

export type ActivityEvent = ToolEvent | ThinkingEvent;

/** Tool names that read as "a command executed" rather than "a tool used" —
 *  the distinction the Claude app's summary line makes. */
function isCommandTool(ev: ToolEvent): boolean {
  return ev.name === "Bash" || ev.name === "BashOutput";
}

export interface ActivitySummary {
  /** True when any tool in the run failed — drives the warning icon. */
  hasError: boolean;
  /** How many tool calls (commands included) ended in an error. */
  failed: number;
  commands: number;
  tools: number;
  thoughts: number;
}

export function summarizeActivity(events: ActivityEvent[]): ActivitySummary {
  let commands = 0;
  let tools = 0;
  let thoughts = 0;
  let failed = 0;

  for (const ev of events) {
    if (ev.kind === "thinking") {
      thoughts += 1;
      continue;
    }
    if (ev.status === "error") failed += 1;
    if (isCommandTool(ev)) commands += 1;
    else tools += 1;
  }

  return { hasError: failed > 0, failed, commands, tools, thoughts };
}

/** The counts above, as one sentence — in whatever language `t` resolves to.
 * Kept apart from `summarizeActivity` so the counting stays a plain, i18n-free
 * unit (the hook `useTranslations` lives in the component, not here — this
 * function is not itself a hook). */
export function toolGroupLabel(
  summary: ActivitySummary,
  t: (key: string, values?: Record<string, string | number | Date>) => string,
): string {
  const parts: string[] = [];
  if (summary.commands > 0) parts.push(t("toolGroup.commands", { count: summary.commands }));
  if (summary.tools > 0) parts.push(t("toolGroup.tools", { count: summary.tools }));
  // The failure used to be carried by colour alone: "35 tools used" with a
  // red triangle read as "the whole run broke" on the operator's phone
  // (04.09.2026) — it was one `mc review` 400 out of 35, retried and fine.
  // Naming the number turns the alarm back into information.
  if (summary.failed > 0) parts.push(t("toolGroup.failed", { count: summary.failed }));
  if (summary.thoughts > 0) parts.push(t("toolGroup.thoughts", { count: summary.thoughts }));

  // Sentence-cases whatever landed first ("thought" → "Thought"; a leading
  // digit is unaffected), so the label reads as one line either way.
  const joined = parts.join(", ");
  return joined.length > 0 ? joined.charAt(0).toUpperCase() + joined.slice(1) : t("toolGroup.activity");
}

function leadingIcon(summary: ActivitySummary) {
  if (summary.hasError) return AlertTriangle;
  if (summary.tools === 0 && summary.commands === 0) return Brain;
  if (summary.tools === 0) return Terminal;
  return Wrench;
}

function renderChild(ev: ActivityEvent, detailLevel: "compact" | "normal" | "verbose") {
  return ev.kind === "tool" ? (
    <ToolRow key={ev.toolUseId ?? ev.uuid} ev={ev} detailLevel={detailLevel} />
  ) : (
    <ThinkingRow key={ev.uuid} ev={ev} detailLevel={detailLevel} />
  );
}

export function ToolGroup({
  events,
  detailLevel = "normal",
}: {
  events: ActivityEvent[];
  detailLevel?: "compact" | "normal" | "verbose";
}) {
  const t = useTranslations("sessions");
  const [expanded, setExpanded] = useState(detailLevel === "verbose");
  // Same re-sync as ToolRow/ThinkingRow (review finding I-3): useState reads
  // its initial value once, so a mounted group would never react to the
  // detail level changing under it. Manual clicks in between survive because
  // this effect doesn't depend on `expanded`.
  useEffect(() => {
    setExpanded(detailLevel === "verbose");
  }, [detailLevel]);

  if (events.length === 0) return null;

  const summary = summarizeActivity(events);
  const label = toolGroupLabel(summary, t);
  const Icon = leadingIcon(summary);

  return (
    <div className="w-full px-4 md:px-5 py-1" data-testid="tool-group">
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
        className="group flex w-full items-center gap-2 rounded-lg px-2.5 text-left min-h-[44px] md:min-h-[34px] cursor-pointer transition-colors"
        style={{
          background: expanded ? C.bgElevated : "transparent",
          border: `1px solid ${C.border}`,
        }}
      >
        {/* Signal doctrine: colour is meaning, not emphasis. The failure gets
            exactly two carriers — the icon's shape/colour and the count — while
            the label stays ordinary text and the frame stays neutral. Painting
            all four red made one bad tool out of sixty read as an alert box.
            And it is a WARNING, not an error: a failed tool inside a run that
            went on is a partial failure — red is reserved for the run itself
            failing (the agent's own error line), amber for „something in here
            went wrong, the number tells you how much". */}
        <Icon
          size={13}
          className="shrink-0"
          style={{ color: summary.hasError ? STATUS_TEXT.warning : C.textMuted }}
          data-testid={summary.hasError ? "tool-group-error-icon" : "tool-group-icon"}
          aria-hidden="true"
        />
        <span className="flex-1 min-w-0 truncate text-[13px] font-medium" style={{ color: C.textSecondary }}>
          {label}
          {/* The failure was carried by colour and an aria-hidden icon alone,
              which is nothing at all to a screen reader. This puts it into the
              button's accessible name without changing the visual line. */}
          {summary.hasError && <span className="sr-only"> — {t("toolGroup.errorIncluded")}</span>}
        </span>
        <span
          className="shrink-0 font-mono text-[10px] font-medium tabular-nums"
          style={{ color: summary.hasError ? STATUS_TEXT.warning : C.textMuted }}
        >
          {events.length}
        </span>
        <ChevronRight
          size={13}
          className="shrink-0 transition-transform duration-150"
          style={{
            color: C.textMuted,
            transform: expanded ? "rotate(90deg)" : undefined,
          }}
          aria-hidden="true"
        />
      </button>

      {expanded && (
        // 1px hairline rail instead of a card: the rows stay in the timeline's
        // own rhythm (Flach-Regel — no nested card), the line just says
        // "these belong to the row above".
        <div
          className="mt-1 ml-3.5 pl-1"
          style={{ borderLeft: `1px solid ${C.border}` }}
          data-testid="tool-group-children"
        >
          {events.map((ev) => renderChild(ev, detailLevel))}
        </div>
      )}
    </div>
  );
}
