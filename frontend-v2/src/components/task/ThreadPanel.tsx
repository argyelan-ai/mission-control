"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Check, CheckCheck, Clock3, SendHorizonal, X } from "lucide-react";
import { api } from "@/lib/api";
import { C } from "@/lib/colors";
import type { TaskThreadResponse, ThreadMessage } from "@/lib/types";

/**
 * THREAD panel (comm_v2, register T12) — user-side chat view on the task thread.
 * Composer is always visible, on every task status incl. `done` (post-`done`
 * delivery is the whole point of comm_v2). No SSE yet: polls a `since_seq`
 * delta every 10 s while mounted (= tab visible), per the API-shape doc.
 */

const POLL_MS = 10_000;
const READ_DEBOUNCE_MS = 2_000;

function mergeAppend(existing: ThreadMessage[], incoming: ThreadMessage[]): ThreadMessage[] {
  if (incoming.length === 0) return existing;
  const seen = new Set(existing.map((m) => m.seq));
  const fresh = incoming.filter((m) => !seen.has(m.seq));
  if (fresh.length === 0) return existing;
  return [...existing, ...fresh].sort((a, b) => a.seq - b.seq);
}

function mergePrepend(existing: ThreadMessage[], older: ThreadMessage[]): ThreadMessage[] {
  const seen = new Set(existing.map((m) => m.seq));
  return [...older.filter((m) => !seen.has(m.seq)), ...existing].sort((a, b) => a.seq - b.seq);
}

function DeliveryMark({ delivery }: { delivery?: ThreadMessage["delivery"] }) {
  if (!delivery) return null;
  const common = "inline-block align-[-2px]";
  switch (delivery) {
    case "read":
      return <CheckCheck size={12} className={common} style={{ color: C.accent }} aria-label="Read" />;
    case "delivered":
      return <Check size={12} className={common} style={{ color: C.textMuted }} aria-label="Delivered" />;
    case "failed":
      return <X size={12} className={common} style={{ color: C.error }} aria-label="Failed" />;
    default:
      return <Clock3 size={11} className={common} style={{ color: C.textDim }} aria-label="Queued" />;
  }
}

function fmtTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleString("en-GB", {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

export function ThreadPanel({ taskId }: { taskId: string }) {
  const [messages, setMessages] = useState<ThreadMessage[]>([]);
  const [recipient, setRecipient] = useState<TaskThreadResponse["recipient"]>(null);
  const [hasMoreBefore, setHasMoreBefore] = useState(false);
  const [myReadSeq, setMyReadSeq] = useState(0);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [unavailable, setUnavailable] = useState(false);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);

  const scrollRef = useRef<HTMLDivElement>(null);
  const stateRef = useRef({ latestSeq: 0, myReadSeq: 0 });
  const readTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const applyResponse = useCallback((res: TaskThreadResponse, mode: "replace" | "append" | "prepend") => {
    setRecipient(res.recipient);
    setHasMoreBefore(res.has_more_before);
    setMyReadSeq(res.my_read_seq);
    stateRef.current = { latestSeq: res.latest_seq, myReadSeq: res.my_read_seq };
    setMessages((prev) =>
      mode === "replace" ? res.messages : mode === "append" ? mergeAppend(prev, res.messages) : mergePrepend(prev, res.messages),
    );
  }, []);

  const scrollToBottom = useCallback(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, []);

  // Initial load: newest page.
  useEffect(() => {
    let cancelled = false;
    api.tasks.thread
      .list(taskId)
      .then((res) => {
        if (cancelled) return;
        applyResponse(res, "replace");
        requestAnimationFrame(scrollToBottom);
      })
      .catch(() => !cancelled && setUnavailable(true));
    return () => {
      cancelled = true;
    };
  }, [taskId, applyResponse, scrollToBottom]);

  // Read-marker: debounced POST when we've seen new messages.
  useEffect(() => {
    const { latestSeq, myReadSeq: read } = stateRef.current;
    if (latestSeq <= read) return;
    if (readTimer.current) clearTimeout(readTimer.current);
    readTimer.current = setTimeout(() => {
      const seq = stateRef.current.latestSeq;
      api.tasks.thread
        .markRead(taskId, seq)
        .then(() => {
          stateRef.current.myReadSeq = seq;
          setMyReadSeq(seq);
        })
        .catch(() => {});
    }, READ_DEBOUNCE_MS);
    return () => {
      if (readTimer.current) clearTimeout(readTimer.current);
    };
  }, [messages, taskId]);

  // Delta polling while mounted (panel visible = tab active).
  useEffect(() => {
    if (unavailable) return;
    const id = setInterval(() => {
      api.tasks.thread
        .list(taskId, { sinceSeq: stateRef.current.latestSeq })
        .then((res) => {
          if (res.messages.length === 0) return;
          applyResponse(res, "append");
          const el = scrollRef.current;
          if (el && el.scrollHeight - el.scrollTop - el.clientHeight < 120) {
            requestAnimationFrame(scrollToBottom);
          }
        })
        .catch(() => {});
    }, POLL_MS);
    return () => clearInterval(id);
  }, [taskId, unavailable, applyResponse, scrollToBottom]);

  const loadOlder = useCallback(() => {
    const first = stateRefFirst(messages);
    if (first == null) return;
    setLoadingOlder(true);
    api.tasks.thread
      .list(taskId, { beforeSeq: first })
      .then((res) => applyResponse(res, "prepend"))
      .catch(() => {})
      .finally(() => setLoadingOlder(false));
  }, [taskId, messages, applyResponse]);

  const send = useCallback(() => {
    const body = draft.trim();
    if (!body || sending) return;
    setSending(true);
    api.tasks.thread
      .post(taskId, body)
      .then(() => {
        setDraft("");
        // POST returns no seq — pull the delta to append our own message.
        return api.tasks.thread.list(taskId, { sinceSeq: stateRef.current.latestSeq });
      })
      .then((res) => {
        applyResponse(res, "append");
        requestAnimationFrame(scrollToBottom);
      })
      .catch(() => {})
      .finally(() => setSending(false));
  }, [draft, sending, taskId, applyResponse, scrollToBottom]);

  if (unavailable) {
    return (
      <div className="px-1 py-6 text-center font-mono text-[11px]" style={{ color: C.textDim }}>
        Thread unavailable — comm_v2 read API not reachable.
      </div>
    );
  }

  // First agent_to_user message beyond the read cursor gets the NEW divider.
  const firstUnread = messages.find((m) => m.direction !== "user_to_agent" && m.seq > myReadSeq)?.seq;

  return (
    <div className="flex flex-col gap-2">
      {/* Recipient line — who reads here right now (server-derived, no guessing) */}
      {recipient && (
        <div className="flex items-center gap-2 font-mono text-[10px] uppercase tracking-[0.08em]" style={{ color: C.textMuted }}>
          <span className="label-sys label-sys--dim">Thread</span>
          <span style={{ color: C.textDim }}>→</span>
          <span style={{ color: C.textSecondary }}>{recipient.display}</span>
          <span
            className="inline-block w-1.5 h-1.5 rounded-full"
            style={{ backgroundColor: recipient.listening ? C.online : C.textDim }}
            aria-label={recipient.listening ? "Listening" : "Not listening"}
          />
          <span style={{ color: C.textDim }}>{recipient.listening ? "listening" : "offline"} · {recipient.reason}</span>
        </div>
      )}

      {/* Messages */}
      <div
        ref={scrollRef}
        className="max-h-[420px] overflow-y-auto rounded-md px-3 py-2 space-y-3"
        style={{ backgroundColor: "var(--color-bg-base)", border: `1px solid ${C.border}` }}
      >
        {hasMoreBefore && (
          <button
            onClick={loadOlder}
            disabled={loadingOlder}
            className="w-full py-1 font-mono text-[10px] uppercase tracking-[0.08em] transition-colors hover:bg-[var(--color-bg-hover)] cursor-pointer disabled:opacity-50"
            style={{ color: C.accent, border: `1px dashed ${C.border}` }}
          >
            {loadingOlder ? "Loading…" : "Load older"}
          </button>
        )}
        {messages.length === 0 && (
          <div className="py-6 text-center font-mono text-[11px]" style={{ color: C.textDim }}>
            No messages yet — start the thread.
          </div>
        )}
        {messages.map((m) => {
          const mine = m.direction === "user_to_agent";
          const system = m.direction === "system";
          return (
            <div key={m.id}>
              {firstUnread === m.seq && (
                <div className="flex items-center gap-2 my-1" aria-label="Unread messages">
                  <span className="flex-1 h-px" style={{ backgroundColor: C.accent }} />
                  <span className="font-mono text-[9px] uppercase tracking-[0.12em]" style={{ color: C.accent }}>
                    New
                  </span>
                  <span className="flex-1 h-px" style={{ backgroundColor: C.accent }} />
                </div>
              )}
              {system ? (
                <div className="text-center font-mono text-[10px] py-0.5" style={{ color: C.textDim }}>
                  {m.body}
                </div>
              ) : (
                <div
                  className="rounded-sm px-2.5 py-1.5 max-w-[92%] sm:max-w-[80%]"
                  style={{
                    marginLeft: mine ? "auto" : 0,
                    backgroundColor: mine ? C.accentSubtle : "var(--color-bg-surface)",
                    border: `1px solid ${mine ? C.borderAccent : C.border}`,
                  }}
                >
                  <div className="flex items-baseline gap-2 font-mono text-[9px] uppercase tracking-[0.08em]" style={{ color: C.textDim }}>
                    <span style={{ color: mine ? C.accent : C.textMuted }}>{mine ? "You" : m.author.display}</span>
                    <span>{fmtTime(m.created_at)}</span>
                    {mine && (
                      <span className="ml-auto">
                        <DeliveryMark delivery={m.delivery} />
                      </span>
                    )}
                  </div>
                  <div className="text-[12.5px] leading-snug whitespace-pre-wrap break-words" style={{ color: C.textPrimary }}>
                    {m.body}
                  </div>
                </div>
              )}
            </div>
          );
        })}
      </div>

      {/* Composer — always visible, on every status incl. done */}
      <div className="flex gap-2">
        <input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              send();
            }
          }}
          placeholder={recipient ? `Message ${recipient.display}… (Enter)` : "Message… (Enter)"}
          aria-label="Thread message"
          className="flex-1 px-2.5 py-2 rounded-md text-xs outline-none min-w-0"
          style={{
            backgroundColor: "var(--color-bg-surface)",
            color: C.textPrimary,
            border: `1px solid ${C.border}`,
          }}
        />
        <button
          onClick={send}
          disabled={!draft.trim() || sending}
          aria-label="Send message"
          className="w-[38px] h-[38px] rounded-md flex items-center justify-center transition-colors cursor-pointer disabled:opacity-40"
          style={{
            backgroundColor: C.accent,
            color: "var(--color-bg-deep)",
          }}
        >
          <SendHorizonal size={15} />
        </button>
      </div>
    </div>
  );
}

function stateRefFirst(messages: ThreadMessage[]): number | null {
  return messages.length > 0 ? messages[0].seq : null;
}
