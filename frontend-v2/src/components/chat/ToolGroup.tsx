"use client";

/**
 * ToolGroup — one tappable summary row standing in for a run of consecutive
 * tool/thinking events, the way the Claude app collapses an agent's working
 * stretch into "3 Befehle ausgeführt, 2 Tools verwendet ›".
 *
 * Why: a transcript's tool rows outnumber its prose by an order of magnitude.
 * Rendering each one is truthful but unreadable — the reader loses the
 * conversation inside the machinery. The group keeps every row (tap to open,
 * `Ausführlich` opens them all) while making the DEFAULT reading experience
 * the conversation itself.
 *
 * Grouping/boundary logic lives in ChatView (`buildTimelineItems`) — this
 * component only renders a run it is handed. `summarizeActivity` is exported
 * because the label is the part with the interesting edge cases (singular vs.
 * plural, thinking-only runs, error aggregation) and deserves its own tests.
 */
import { useEffect, useState } from "react";
import { AlertTriangle, Brain, ChevronRight, Terminal, Wrench } from "lucide-react";
import { C, STATUS_TEXT } from "@/lib/colors";
import type { ThinkingEvent, ToolEvent } from "@/lib/chatTypes";
import { ToolRow } from "./ToolRow";
import { ThinkingRow } from "./ThinkingRow";

export type ActivityEvent = ToolEvent | ThinkingEvent;

/** Tool names that read as "einen Befehl ausgeführt" rather than "ein Tool
 *  verwendet" — the distinction the Claude app's summary line makes. */
function isCommandTool(ev: ToolEvent): boolean {
  return ev.name === "Bash" || ev.name === "BashOutput";
}

export interface ActivitySummary {
  label: string;
  /** True when any tool in the run failed — drives the warning icon. */
  hasError: boolean;
  commands: number;
  tools: number;
  thoughts: number;
}

export function summarizeActivity(events: ActivityEvent[]): ActivitySummary {
  let commands = 0;
  let tools = 0;
  let thoughts = 0;
  let hasError = false;

  for (const ev of events) {
    if (ev.kind === "thinking") {
      thoughts += 1;
      continue;
    }
    if (ev.status === "error") hasError = true;
    if (isCommandTool(ev)) commands += 1;
    else tools += 1;
  }

  const parts: string[] = [];
  if (commands > 0) parts.push(`${commands} ${commands === 1 ? "Befehl" : "Befehle"} ausgeführt`);
  if (tools > 0) parts.push(`${tools} ${tools === 1 ? "Tool" : "Tools"} verwendet`);
  if (thoughts > 0) parts.push(thoughts === 1 ? "nachgedacht" : `${thoughts}× nachgedacht`);

  // Sentence-cases whatever landed first ("nachgedacht" → "Nachgedacht"; a
  // leading digit is unaffected), so the label reads as one line either way.
  const joined = parts.join(", ");
  const label = joined.length > 0 ? joined.charAt(0).toUpperCase() + joined.slice(1) : "Aktivität";

  return { label, hasError, commands, tools, thoughts };
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
  const Icon = leadingIcon(summary);
  const labelColor = summary.hasError ? STATUS_TEXT.error : C.textSecondary;

  return (
    <div className="w-full px-4 md:px-5 py-1" data-testid="tool-group">
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
        className="group flex w-full items-center gap-2 rounded-lg px-2.5 text-left min-h-[44px] md:min-h-[34px] cursor-pointer transition-colors"
        // The failure is carried by the icon's shape and the label's colour,
        // not by tinting the frame: one failed tool out of sixty should not
        // make the whole run read as a red alert box.
        style={{
          background: expanded ? C.bgSurface : "transparent",
          border: `1px solid ${C.border}`,
        }}
      >
        <Icon
          size={13}
          className="shrink-0"
          style={{ color: summary.hasError ? STATUS_TEXT.error : C.textMuted }}
          data-testid={summary.hasError ? "tool-group-error-icon" : "tool-group-icon"}
          aria-hidden="true"
        />
        <span className="flex-1 min-w-0 truncate text-[13px] font-medium" style={{ color: labelColor }}>
          {summary.label}
        </span>
        <span
          className="shrink-0 font-mono text-[10px] font-medium tabular-nums"
          style={{ color: C.textMuted }}
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
