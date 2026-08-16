"use client";

import { Brain } from "lucide-react";
import { C } from "@/lib/colors";
import type { ThinkingEvent } from "@/lib/chatTypes";

/**
 * Muted, italic single row for extended-thinking blocks — quieter than a
 * message or tool row since it's the agent's internal reasoning, not
 * output. No expand/collapse: thinking text is short-form by construction.
 */
export function ThinkingRow({ ev }: { ev: ThinkingEvent }) {
  return (
    <div className="w-full px-4 py-1.5 flex items-start gap-2">
      <Brain size={13} className="shrink-0 mt-0.5" style={{ color: C.textDim }} />
      <span className="text-[13px] italic leading-relaxed" style={{ color: C.textDim }}>
        {ev.text}
      </span>
    </div>
  );
}
