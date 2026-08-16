"use client";

import { useState } from "react";
import { Bot, ChevronRight, ChevronDown } from "lucide-react";
import { C } from "@/lib/colors";
import type { ChatEvent } from "@/lib/chatTypes";
import { ChatMessage } from "./ChatMessage";
import { ToolRow } from "./ToolRow";
import { ThinkingRow } from "./ThinkingRow";

const TITLE_MAX_LEN = 60;

function truncate(text: string): string {
  const firstLine = text.split("\n")[0]?.trim() ?? "";
  return firstLine.length > TITLE_MAX_LEN ? `${firstLine.slice(0, TITLE_MAX_LEN)}…` : firstLine;
}

function eventTitle(ev: ChatEvent): string {
  switch (ev.kind) {
    case "tool":
      return ev.title;
    case "message":
      return truncate(ev.text);
    case "thinking":
      return truncate(ev.text);
    case "command":
      return truncate(ev.command);
    default:
      return "Subagent";
  }
}

/** Renders one child event with the same row components used in the main
 *  timeline. `usage`/`state`/`session_changed` carry no visible content
 *  inside a sidechain and are skipped. */
function renderChildEvent(ev: ChatEvent) {
  switch (ev.kind) {
    case "message":
      return <ChatMessage key={ev.uuid} ev={ev} />;
    case "tool":
      return <ToolRow key={ev.toolUseId ?? ev.uuid} ev={ev} detailLevel="normal" />;
    case "thinking":
      return <ThinkingRow key={ev.uuid} ev={ev} detailLevel="normal" />;
    case "command":
      return (
        <div key={ev.uuid} className="w-full px-4 py-1.5 text-xs font-mono" style={{ color: C.textMuted }}>
          {ev.command}
        </div>
      );
    default:
      return null;
  }
}

export function SubagentGroup({ events }: { events: ChatEvent[] }) {
  const [expanded, setExpanded] = useState(false);

  if (events.length === 0) return null;

  const headerTitle = eventTitle(events[0]);
  const Chevron = expanded ? ChevronDown : ChevronRight;

  return (
    <div
      className="w-full mx-4 my-2 rounded overflow-hidden"
      style={{ border: `1px solid ${C.border}`, background: C.bgSurface, width: "calc(100% - 2rem)" }}
    >
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="w-full flex items-center gap-2 px-3 py-2 text-left bg-transparent border-0 cursor-pointer"
      >
        <Chevron size={13} className="shrink-0" style={{ color: C.textMuted }} />
        <Bot size={13} className="shrink-0" style={{ color: C.accent }} />
        <span className="text-[13px] truncate" style={{ color: C.textSecondary }}>
          Agent: {headerTitle}
        </span>
        <span
          className="ml-auto shrink-0 text-[10px] font-mono px-1.5 py-0.5 rounded"
          style={{ background: C.bgHover, color: C.textMuted }}
        >
          {events.length}
        </span>
      </button>

      {expanded && (
        <div style={{ borderTop: `1px solid ${C.border}` }}>
          {events.map(renderChildEvent)}
        </div>
      )}
    </div>
  );
}
