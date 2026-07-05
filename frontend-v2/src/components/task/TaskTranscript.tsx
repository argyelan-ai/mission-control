"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Radio, Info } from "lucide-react";
import { api } from "@/lib/api";
import { timeAgo } from "@/lib/utils";
import type { TranscriptMessage as TranscriptMessageType } from "@/lib/types";
import { C } from "@/lib/colors";

const COMMENT_TYPE_LABELS: Record<string, string> = {
  handoff: "Handoff",
  blocker: "Blocker",
  progress: "Progress",
  resolution: "Resolution",
  feedback: "Feedback",
};

// ── Transcript Message ──────────────────────────────────────────────────────

function TranscriptMessage({ message }: { message: TranscriptMessageType }) {
  const [expanded, setExpanded] = useState(false);
  const isAgent = message.role === "agent";
  const content = message.content ?? "";
  const typeLabel =
    message.comment_type && message.comment_type !== "message"
      ? COMMENT_TYPE_LABELS[message.comment_type] ?? message.comment_type
      : null;

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
        {typeLabel && (
          <span
            className="text-[10px] font-semibold px-1.5 py-0.5 rounded"
            style={{ backgroundColor: C.accentSubtle, color: C.accent }}
          >
            {typeLabel}
          </span>
        )}
        {message.ts && (
          <span className="text-[10px]" style={{ color: C.textMuted }}>
            {timeAgo(message.ts)}
          </span>
        )}
      </div>
      <pre
        className="whitespace-pre-wrap text-xs leading-relaxed break-words"
        style={{
          color: C.textPrimary,
          fontFamily: "var(--font-geist-mono, monospace)",
        }}
      >
        {content.length > 600 && !expanded ? (
          <>
            {content.slice(0, 600)}...
            <button
              onClick={() => setExpanded(true)}
              className="ml-1 underline cursor-pointer"
              style={{ color: C.accent }}
            >
              mehr
            </button>
          </>
        ) : (
          content
        )}
      </pre>
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

  const messages = data.messages ?? [];

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
        {data.transcript_mode === "taskcomment" && isLive ? (
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
      {messages.length === 0 ? (
        <div className="text-xs" style={{ color: C.textMuted }}>
          Session empty -- no messages yet.
        </div>
      ) : (
        messages.map((msg, i) => (
          <TranscriptMessage key={i} message={msg} />
        ))
      )}
    </div>
  );
}
