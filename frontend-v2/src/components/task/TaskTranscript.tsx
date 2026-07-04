"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Radio, Info, ChevronRight } from "lucide-react";
import { api } from "@/lib/api";
import { timeAgo } from "@/lib/utils";
import type { GatewayMessage, GatewayMessagePart } from "@/lib/types";
import { C } from "@/lib/colors";

// ── Transcript Part ──────────────────────────────────────────────────────────

function TranscriptPart({ part }: { part: GatewayMessagePart }) {
  const [expanded, setExpanded] = useState(false);

  if (part.type === "text" && part.text) {
    return (
      <pre
        className="whitespace-pre-wrap text-xs leading-relaxed break-words"
        style={{
          color: C.textPrimary,
          fontFamily: "var(--font-geist-mono, monospace)",
        }}
      >
        {part.text.length > 600 && !expanded ? (
          <>
            {part.text.slice(0, 600)}...
            <button
              onClick={() => setExpanded(true)}
              className="ml-1 underline cursor-pointer"
              style={{ color: C.accent }}
            >
              mehr
            </button>
          </>
        ) : (
          part.text
        )}
      </pre>
    );
  }

  if (part.type === "thinking") {
    return (
      <button
        onClick={() => setExpanded(!expanded)}
        className="flex flex-col items-start gap-1 text-[10px] w-full text-left cursor-pointer"
        style={{ color: C.textMuted }}
      >
        <div className="flex items-center gap-1">
          <ChevronRight
            size={10}
            className="transition-transform"
            style={{ transform: expanded ? "rotate(90deg)" : "rotate(0deg)" }}
          />
          <span>Thinking...</span>
        </div>
        {expanded && part.text && (
          <pre
            className="whitespace-pre-wrap text-xs mt-1 pl-3 border-l break-words"
            style={{
              color: C.textSecondary,
              borderColor: C.border,
              fontFamily: "var(--font-geist-mono, monospace)",
            }}
          >
            {part.text.slice(0, 800)}
            {part.text.length > 800 && "..."}
          </pre>
        )}
      </button>
    );
  }

  if (part.type === "tool_call") {
    return (
      <div className="flex items-center gap-1.5">
        <span
          className="text-[10px] font-semibold px-1.5 py-0.5 rounded"
          style={{
            backgroundColor: C.accentSubtle,
            color: C.accent,
          }}
        >
          {part.tool || "tool"}
        </span>
        {part.summary && (
          <span
            className="text-[10px] truncate"
            style={{ color: C.textMuted }}
          >
            {part.summary}
          </span>
        )}
      </div>
    );
  }

  if (part.type === "tool_result") {
    return (
      <button
        onClick={() => setExpanded(!expanded)}
        className="flex flex-col w-full text-left cursor-pointer"
      >
        <div
          className="flex items-center gap-1 text-[10px]"
          style={{ color: C.textMuted }}
        >
          <ChevronRight
            size={10}
            className="transition-transform"
            style={{ transform: expanded ? "rotate(90deg)" : "rotate(0deg)" }}
          />
          <span>{part.is_error ? "Error" : "Result"}</span>
        </div>
        {expanded && part.text && (
          <pre
            className="whitespace-pre-wrap text-[10px] mt-1 pl-3 border-l break-words max-h-40 overflow-y-auto"
            style={{
              color: part.is_error ? C.error : C.textSecondary,
              borderColor: C.border,
              fontFamily: "var(--font-geist-mono, monospace)",
            }}
          >
            {part.text.slice(0, 1200)}
            {part.text.length > 1200 && "..."}
          </pre>
        )}
      </button>
    );
  }

  return null;
}

// ── Transcript Message ──────────────────────────────────────────────────────

function TranscriptMessage({ message }: { message: GatewayMessage }) {
  const isAgent = message.role === "assistant";

  return (
    <div
      className="rounded-lg p-2.5 text-xs"
      style={{
        backgroundColor: isAgent
          ? C.accentSubtle
          : "rgba(255, 255, 255, 0.02)",
        border: `1px solid ${isAgent ? C.borderAccent : C.border}`,
      }}
    >
      <div className="flex items-center gap-1.5 mb-1.5">
        <span
          className="text-[10px] font-semibold uppercase tracking-[0.06em]"
          style={{
            color: isAgent ? C.accent : C.textMuted,
          }}
        >
          {isAgent ? "Agent" : "System"}
        </span>
        {message.timestamp && (
          <span className="text-[10px]" style={{ color: C.textMuted }}>
            {timeAgo(message.timestamp)}
          </span>
        )}
      </div>
      <div className="space-y-1">
        {message.parts.map((part, j) => (
          <TranscriptPart key={j} part={part} />
        ))}
      </div>
    </div>
  );
}

// ── TaskTranscript ──────────────────────────────────────────────────────────

interface TaskTranscriptProps {
  taskId: string;
  isLive: boolean;
}

export function TaskTranscript({ taskId, isLive }: TaskTranscriptProps) {
  const { data, isLoading } = useQuery({
    queryKey: ["task-transcript", taskId],
    queryFn: () => api.tasks.transcript(taskId, 30),
    refetchInterval: isLive ? 10_000 : false,
  });

  if (isLoading) {
    return (
      <div className="text-xs" style={{ color: C.textMuted }}>
        Loading transcript...
      </div>
    );
  }

  if (!data || data.transcript_mode === "unavailable") {
    return (
      <div className="text-xs" style={{ color: C.textMuted }}>
        No transcript available.
      </div>
    );
  }

  return (
    <div className="space-y-2">
      {/* Meta badge */}
      <div
        className="flex items-center gap-2 text-[10px] px-2.5 py-1.5 rounded-lg"
        style={{
          backgroundColor: "rgba(255, 255, 255, 0.02)",
          border: `1px solid ${C.border}`,
          color: C.textMuted,
        }}
      >
        {data.transcript_mode === "direct" && isLive ? (
          <>
            <Radio size={10} style={{ color: C.online }} />
            <span style={{ color: C.online }}>
              Live
            </span>
          </>
        ) : data.transcript_mode === "reconstructed" ? (
          <>
            <Info size={10} />
            <span>
              Last known session
              {data.session_role ? ` (${data.session_role})` : ""}
            </span>
          </>
        ) : (
          <span>Current session</span>
        )}
      </div>

      {/* Messages */}
      {data.messages.length === 0 ? (
        <div className="text-xs" style={{ color: C.textMuted }}>
          Session empty -- no messages yet.
        </div>
      ) : (
        data.messages.map((msg, i) => (
          <TranscriptMessage key={i} message={msg} />
        ))
      )}
    </div>
  );
}
