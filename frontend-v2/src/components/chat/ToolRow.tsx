"use client";

import { useState } from "react";
import { FileText, Pencil, Terminal, Globe, Bot, Wrench } from "lucide-react";
import { C, STATUS_TEXT } from "@/lib/colors";
import type { ToolEvent } from "@/lib/chatTypes";

const TOOL_ICON: Record<string, typeof Wrench> = {
  Read: FileText,
  Edit: Pencil,
  Write: Pencil,
  Bash: Terminal,
  WebSearch: Globe,
  WebFetch: Globe,
  Task: Bot,
};

function iconFor(name: string) {
  return TOOL_ICON[name] ?? Wrench;
}

export function ToolRow({
  ev,
  detailLevel,
}: {
  ev: ToolEvent;
  detailLevel: "compact" | "normal" | "verbose";
}) {
  const [expanded, setExpanded] = useState(detailLevel === "verbose");
  const Icon = iconFor(ev.name);
  const isError = ev.status === "error";

  return (
    <div className="w-full px-4 py-1.5">
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="w-full flex items-center gap-2 text-left bg-transparent border-0 p-0 cursor-pointer"
      >
        <Icon size={13} className="shrink-0" style={{ color: C.textMuted }} />
        <span
          className="text-xs font-mono truncate"
          style={{ color: isError ? STATUS_TEXT.error : C.textSecondary }}
        >
          {ev.title}
        </span>

        {ev.stats && (
          <span className="shrink-0 flex items-center gap-1.5 ml-auto">
            {ev.stats.additions > 0 && (
              <span className="text-[10px] font-mono font-medium" style={{ color: STATUS_TEXT.online }}>
                +{ev.stats.additions}
              </span>
            )}
            {ev.stats.deletions > 0 && (
              <span className="text-[10px] font-mono font-medium" style={{ color: STATUS_TEXT.error }}>
                −{ev.stats.deletions}
              </span>
            )}
          </span>
        )}

        {isError && (
          <span
            data-testid="tool-row-error-dot"
            className="inline-block w-1.5 h-1.5 rounded-full shrink-0"
            style={{ background: STATUS_TEXT.error, marginLeft: ev.stats ? undefined : "auto" }}
            aria-label="Fehler"
          />
        )}
      </button>

      {expanded && (
        <div className="mt-1.5 ml-5 space-y-1.5">
          <pre
            className="max-h-[320px] overflow-auto text-[11px] font-mono p-2 rounded"
            style={{ background: "var(--color-bg-elevated)", color: C.textMuted, border: `1px solid ${C.border}` }}
          >
            {JSON.stringify(ev.detail, null, 2)}
          </pre>
          {ev.result != null && (
            <pre
              className="max-h-[320px] overflow-auto text-[11px] font-mono p-2 rounded"
              style={{
                background: "var(--color-bg-elevated)",
                color: isError ? STATUS_TEXT.error : C.textSecondary,
                border: `1px solid ${isError ? C.error : C.border}`,
              }}
            >
              {ev.result}
            </pre>
          )}
        </div>
      )}
    </div>
  );
}
