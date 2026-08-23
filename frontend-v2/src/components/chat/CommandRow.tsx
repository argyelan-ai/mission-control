"use client";

/**
 * One slash command the operator ran inside the session — and, when the backend
 * managed to merge it, the output it produced.
 *
 * The output half is not cosmetic: `/context`, `/status` and every skill command
 * exist ONLY for what they print. Rendering the command alone (which is what the
 * timeline did before `CommandEvent.result` shipped) meant running `/context`
 * showed the words "/context" and nothing else — the answer was parsed by the
 * backend and then dropped on the floor.
 *
 * Collapsed by default like ToolRow: a command's output is reference material,
 * not conversation. `Ausführlich` opens it, the same as every other detail in
 * this timeline.
 */
import { useEffect, useState } from "react";
import { ChevronRight, Terminal } from "lucide-react";
import { C } from "@/lib/colors";
import type { CommandEvent } from "@/lib/chatTypes";

export function CommandRow({
  ev,
  detailLevel = "normal",
}: {
  ev: CommandEvent;
  detailLevel?: "compact" | "normal" | "verbose";
}) {
  const [expanded, setExpanded] = useState(detailLevel === "verbose");
  // Same re-sync as ToolRow/ThinkingRow/ToolGroup (review finding I-3).
  useEffect(() => {
    setExpanded(detailLevel === "verbose");
  }, [detailLevel]);

  const result = ev.result?.trim() ?? "";
  const hasResult = result.length > 0;

  // No output to show (or none merged yet) — stay the plain one-liner the
  // timeline always had, rather than growing a chevron that reveals nothing.
  if (!hasResult) {
    return (
      <div className="w-full px-4 md:px-5 py-1.5 text-xs font-mono" style={{ color: C.textMuted }}>
        {ev.command}
      </div>
    );
  }

  return (
    <div className="w-full px-4 md:px-5 py-1" data-testid="command-row">
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
        className="flex w-full items-center gap-2 text-left bg-transparent border-0 p-0 cursor-pointer"
      >
        <Terminal size={13} className="shrink-0" style={{ color: C.textMuted }} aria-hidden="true" />
        <span className="text-xs font-mono truncate" style={{ color: C.textSecondary }}>
          {ev.command}
        </span>
        <ChevronRight
          size={13}
          className="shrink-0 ml-auto transition-transform duration-150"
          style={{ color: C.textMuted, transform: expanded ? "rotate(90deg)" : undefined }}
          aria-hidden="true"
        />
      </button>

      {expanded && (
        <pre
          data-testid="command-row-result"
          className="mt-1.5 ml-5 max-h-[320px] overflow-auto scroll-quiet whitespace-pre-wrap text-xs font-mono p-2 rounded-sm"
          style={{ background: C.bgElevated, color: C.textSecondary, border: `1px solid ${C.border}` }}
        >
          {result}
        </pre>
      )}
    </div>
  );
}
