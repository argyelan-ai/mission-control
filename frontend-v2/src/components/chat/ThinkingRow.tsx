"use client";

import { useEffect, useState } from "react";
import { Brain } from "lucide-react";
import { C } from "@/lib/colors";
import type { ThinkingEvent } from "@/lib/chatTypes";

/**
 * Collapsed-by-default row for extended-thinking blocks — quieter than a
 * message or tool row since it's the agent's internal reasoning, not
 * output. Click reveals the full text, same expand interaction as ToolRow.
 */
export function ThinkingRow({
  ev,
  detailLevel = "normal",
}: {
  ev: ThinkingEvent;
  detailLevel?: "compact" | "normal" | "verbose";
}) {
  const [expanded, setExpanded] = useState(detailLevel === "verbose");
  // See ToolRow.tsx: useState's initial value is read once, so a mounted
  // row never reacted to detailLevel changing under it. Re-sync on every
  // detailLevel change; manual clicks in between are untouched (I-3).
  useEffect(() => {
    setExpanded(detailLevel === "verbose");
  }, [detailLevel]);

  return (
    <div className="w-full px-4 py-1.5">
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="w-full flex items-center gap-2 text-left bg-transparent border-0 p-0 cursor-pointer"
      >
        {/* textDim (#666) is a decoration tone — 3.5:1 on this background. The
            quiet register here comes from italics and the collapsed row, not
            from dropping the label under AA. */}
        <Brain size={13} className="shrink-0" style={{ color: C.textDim }} />
        <span className="text-[13px] italic" style={{ color: C.textMuted }}>
          Denkt nach…
        </span>
      </button>

      {expanded && (
        <pre
          className="mt-1.5 ml-5 max-h-[320px] overflow-auto whitespace-pre-wrap text-[13px] italic leading-relaxed font-sans p-2 rounded scroll-quiet"
          style={{ background: "var(--color-bg-elevated)", color: C.textMuted, border: `1px solid ${C.border}` }}
        >
          {ev.text}
        </pre>
      )}
    </div>
  );
}
