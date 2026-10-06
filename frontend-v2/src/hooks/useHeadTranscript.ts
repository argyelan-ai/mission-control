"use client";

/**
 * `useHeadTranscript` — a head's transcript, read-only, shaped so
 * `ChatView` can treat it exactly like `useChatStream`'s result (bauplan
 * `heads-sichtbar` PR 2 §3.2: "Liefert dieselbe Form wie
 * `UseChatStreamResult`"). There is no live stream for a head (ADR-085
 * Nachtrag 2026-10-04 §4: files only, no tailer, no SSE) — "live" here
 * means "polled every 5 s while the run is still active", via
 * `GET /heads/{run_id}/chat/history`'s own ETag: a 304 means nothing to
 * re-render, so the hook simply keeps the page it already had rather than
 * ever showing an empty transcript while polling.
 *
 * The echo/composer half of `UseChatStreamResult` (`pendingEchoes`,
 * `echoSent`, …) has no head equivalent — a head has no composer at all —
 * so those fields are fixed no-ops/empties. `ChatView`'s head branch never
 * calls them; they exist purely so `stream` can be ONE variable of ONE
 * type regardless of which hook produced it (`head ? headStream :
 * agentStream`), rather than two incompatible shapes the rendering code
 * would have to branch on everywhere.
 */
import { useEffect, useReducer, useRef } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { HeadChatHistoryResponse } from "@/lib/heads";
import { HEAD_ACTIVE_STATES, type HeadState } from "@/lib/heads";
import { chatReducer, createInitialChatState, type PendingEcho } from "./useChatStream";
import type { SubagentRun } from "@/lib/chatTypes";
import type { UseChatStreamResult } from "./useChatStream";

/** How often the page re-fetches while the head is still active. Matches
 *  the bauplan's own number (§3.2: "alle 5 s nur solange der Head aktiv
 *  ist") — a head writes its transcript far less often than an agent's
 *  live tailer ticks, so this is slower than `useChatStream`'s SSE push on
 *  purpose, not an oversight. */
export const HEAD_TRANSCRIPT_POLL_MS = 5_000;

/** Pure so the "poll only while active" rule is testable without waiting
 *  out a real (or faked) 5s timer — `null` state means "don't know yet,
 *  assume active" (the caller has not loaded the run's state at all, e.g.
 *  a fresh mount), which is the safer default: it costs one extra poll on
 *  an already-ended run, never a silently-stopped one that is still
 *  running. */
export function isHeadTranscriptActive(runId: string | null, headState: HeadState | null): boolean {
  return runId != null && (headState == null || HEAD_ACTIVE_STATES.has(headState));
}

const EMPTY_RUNS: SubagentRun[] = [];
const NO_OP = () => {};
const NO_OP_SENT = (_text: string, _midTurn?: boolean) => {};
const NO_OP_STARTING = (_text: string, _retry: () => void) => {};
const NO_OP_WITHDRAW = (): string[] => [];

export function useHeadTranscript(runId: string | null, headState: HeadState | null = null): UseChatStreamResult {
  const qc = useQueryClient();
  const [chatState, dispatch] = useReducer(chatReducer, undefined, createInitialChatState);
  const seededAtRef = useRef(0);
  const etagRef = useRef<string | null>(null);
  // Carries the last successfully-read page across a 304: `api.heads.
  // history` returns `null` on 304 (nothing to parse, see its own
  // docstring), and a plain "return null from queryFn" would make
  // TanStack Query (correctly) treat that as "no data", blanking out an
  // already-loaded transcript every 5s poll that happens to land on an
  // unchanged file.
  const lastPageRef = useRef<HeadChatHistoryResponse | null>(null);
  // Reset both refs the render `runId` changes — mutating a ref during
  // render (not in an effect) is the React-sanctioned way to "adjust state
  // when a prop changes" without an extra render; it must happen HERE,
  // before `useQuery` below runs `queryFn` for the new id, or the new run
  // would start out replaying the previous run's last page on its first
  // 304 (which cannot happen anyway, since `etagRef` is cleared with it,
  // but keeping the two resets together avoids relying on that).
  const prevRunIdRef = useRef<string | null>(null);
  if (prevRunIdRef.current !== runId) {
    prevRunIdRef.current = runId;
    etagRef.current = null;
    lastPageRef.current = null;
    seededAtRef.current = 0;
    // `chatState` is otherwise keyed on nothing but this hook's own mount —
    // switching `runId` without this left the PREVIOUS run's events on
    // screen until the new run's first page happened to arrive (caught by
    // `useHeadTranscript.test.tsx`'s "switching to a new run id" case: it
    // failed red, showing both runs' messages concatenated, before this
    // line existed). `session_changed` is the exact reset `useChatStream`
    // already uses for the same "new transcript, old one does not apply"
    // moment (its own `chatReducer` case). Dispatching HERE, during render
    // rather than in a `useEffect`, is the React-sanctioned way to adjust
    // state when a prop changes without an extra, visibly-stale frame —
    // the same pattern this function already uses for the two plain refs
    // above.
    dispatch({ kind: "session_changed" });
  }

  const query = useQuery({
    queryKey: ["heads", runId, "chat", "history"],
    queryFn: async () => {
      const res = await api.heads.history(runId as string, { limit: 1000, etag: etagRef.current });
      if (res === null) return lastPageRef.current as HeadChatHistoryResponse;
      etagRef.current = res.etag;
      lastPageRef.current = res.data;
      return res.data;
    },
    enabled: runId != null,
    // A head's own backend decides freshness via the ETag it sends back —
    // there is nothing here a client-side staleTime guess would improve.
    // `refetchInterval` below is the one thing driving a re-fetch.
    staleTime: Infinity,
    refetchOnWindowFocus: false,
    refetchOnMount: "always",
    refetchInterval: isHeadTranscriptActive(runId, headState) ? HEAD_TRANSCRIPT_POLL_MS : false,
  });

  useEffect(() => {
    const active = isHeadTranscriptActive(runId, headState);
    // The run just ended (its OWN last status change, not a parent
    // re-render) — one more fetch to pick up the final page the interval
    // above no longer schedules.
    if (!active && runId != null) qc.invalidateQueries({ queryKey: ["heads", runId, "chat", "history"] });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [headState, runId]);

  useEffect(() => {
    const data = query.data;
    if (!data) return;
    if (seededAtRef.current === query.dataUpdatedAt) return;
    seededAtRef.current = query.dataUpdatedAt;
    // Idempotent: `chatReducer`/`pushOrReplace` dedup on the event's own
    // key, so re-dispatching the exact same page after a 304 (same object,
    // new `dataUpdatedAt`) costs a little work, never a wrong result.
    for (const ev of data.events) dispatch(ev);
  }, [query.data, query.dataUpdatedAt]);

  return {
    events: chatState.events,
    subagentRuns: query.data?.subagentRuns ?? EMPTY_RUNS,
    state: null, // a head has no "idle/working/permission_prompt" state frame — StatusLine/Composer are hidden entirely in head mode
    usage: null, // heads carry no per-turn usage frame
    session: query.data?.session ?? null,
    hasMore: query.data?.hasMore ?? false,
    connected: !query.isError,
    loading: query.isLoading,
    error: query.isError ? (query.error as Error) : null,
    capabilities: null, // nothing to switch — a head has no model/effort picker
    pendingEchoes: [] as PendingEcho[],
    echoSent: NO_OP_SENT,
    echoFailed: NO_OP,
    echoAgentStarting: NO_OP_STARTING,
    withdrawQueued: NO_OP_WITHDRAW,
    awaitingResponse: false,
    preview: null,
  };
}

/** The response's `source`/`reader`/`reason` — fields an agent's own
 *  history never carries, which `ChatView`'s head branch needs to pick
 *  between the normal transcript view and the "no reader for this
 *  harness" fallback (bauplan §7: "Heads ohne Leser (kimi, grok):
 *  `source:"none"`"). Read straight from the query cache rather than added
 *  to `UseChatStreamResult` itself — that type is shared with agents, which
 *  have no such concept at all. */
export function useHeadTranscriptMeta(
  runId: string | null,
): { source: "transcript" | "none" | null; reader: string | null; reason: string | null } {
  const qc = useQueryClient();
  const cached = runId ? qc.getQueryData<HeadChatHistoryResponse>(["heads", runId, "chat", "history"]) : undefined;
  if (!cached) return { source: null, reader: null, reason: null };
  return { source: cached.source, reader: cached.reader, reason: cached.reason };
}
