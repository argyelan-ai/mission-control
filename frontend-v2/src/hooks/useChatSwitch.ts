"use client";

import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, type RecordChat, type SwitchRow } from "@/lib/api";
import type { MessageEvent, TimelineChatEvent } from "@/lib/chatTypes";

// ── Chat switch (ADR-105, S3 item D) ────────────────────────────────────────
//
// A switch lives in MC's record (`chat_events` kind=switch), not in the
// harness transcript — so it is read from the record and laid into the
// timeline here, at its time. Everything below only runs while the record
// exists (CHAT_RECORD_ENABLED): the record answers 404 otherwise, the list
// query fails, and the timeline is exactly the one without this code.

/** Shared with the chat list (useChatList) — one request serves both. */
export const RECORD_CHATS_QUERY_KEY = ["chat-record", "chats"] as const;

export function recordSwitchesQueryKey(chatId: string | null) {
  return ["chat-record", "switches", chatId] as const;
}

/** How often the switch rows are read: fast while one is open (the harness
 *  reports back within seconds), calm otherwise. */
export const SWITCH_POLL_PENDING_MS = 3_000;
export const SWITCH_POLL_IDLE_MS = 60_000;

export interface ChatSwitchState {
  /** The record is on AND this agent's chat is in it. Off = no switch UI. */
  enabled: boolean;
  /** The record chat this view shows: today always the agent's own
   *  (default) chat — the chat view has no other one yet. */
  chat: RecordChat | null;
  /** Every chat of the record (the picker counts who a box switch hits). */
  allChats: RecordChat[];
  /** Switch rows of the chat, oldest first. */
  switches: SwitchRow[];
  /** The open switch, if any. */
  pending: SwitchRow | null;
}

const NO_SWITCHES: SwitchRow[] = [];
const NO_CHATS: RecordChat[] = [];

/** The switch rows inside one page of record history. */
export function switchRowsOf(events: { id: string; seq: number; event: Record<string, unknown> | null }[]): SwitchRow[] {
  const rows: SwitchRow[] = [];
  for (const e of events) {
    const p = e.event;
    if (!p || p.kind !== "switch") continue;
    rows.push({ ...(p as unknown as SwitchRow), eventId: e.id, seq: e.seq });
  }
  return rows;
}

/** The agent's own chat in the record (the one this view shows). */
export function defaultRecordChat(chats: RecordChat[] | undefined, agentId: string | null): RecordChat | null {
  if (!agentId || !Array.isArray(chats)) return null;
  return chats.find((c) => c.agentDefault && c.legacyAgentId === agentId) ?? null;
}

export function useChatSwitch(agentId: string | null): ChatSwitchState {
  const list = useQuery({
    queryKey: RECORD_CHATS_QUERY_KEY,
    queryFn: () => api.chatRecord.listChats(),
    enabled: !!agentId,
    retry: false,
    staleTime: 30_000,
    refetchOnWindowFocus: false,
  });
  const allChats = list.isSuccess && Array.isArray(list.data?.chats) ? list.data.chats : NO_CHATS;
  const chat = defaultRecordChat(allChats, agentId);
  const chatId = chat?.id ?? null;
  const rowsQuery = useQuery({
    queryKey: recordSwitchesQueryKey(chatId),
    queryFn: async () => switchRowsOf((await api.chatRecord.history(chatId as string)).events ?? []),
    enabled: !!chatId,
    retry: false,
    refetchOnWindowFocus: false,
    refetchInterval: (query) =>
      (query.state.data ?? []).some((r) => r.status === "pending") ? SWITCH_POLL_PENDING_MS : SWITCH_POLL_IDLE_MS,
  });
  const switches = rowsQuery.data ?? NO_SWITCHES;
  const pending = useMemo(() => [...switches].reverse().find((r) => r.status === "pending") ?? null, [switches]);
  return { enabled: !!chat, chat, allChats, switches, pending };
}

/** The timeline line of one switch row — a `teammate` message of source
 *  `switch` (ChatMessage draws it as a SwitchCard). `latest`: only the
 *  newest switch offers "switch back". */
export function switchEvent(row: SwitchRow, chatId: string, latest: boolean): MessageEvent {
  return {
    kind: "message",
    uuid: `switch:${row.eventId}`,
    ts: row.ts,
    role: "teammate",
    text: "",
    model: null,
    sidechain: false,
    source: { kind: "switch", title: null },
    switchInfo: { row, chatId, latest },
  };
}

function tsOf(ts: string | null | undefined): number {
  const n = ts ? Date.parse(ts) : NaN;
  return Number.isNaN(n) ? Number.POSITIVE_INFINITY : n;
}

/** Lays the switch rows into the transcript at their time: before the first
 *  event that is newer. No rows = the very same array (no re-render). */
export function mergeSwitchEvents(
  events: TimelineChatEvent[],
  rows: SwitchRow[],
  chatId: string | null,
): TimelineChatEvent[] {
  if (!chatId || rows.length === 0) return events;
  const lastId = rows[rows.length - 1].eventId;
  const cards = rows
    .map((r) => switchEvent(r, chatId, r.eventId === lastId))
    .sort((a, b) => tsOf(a.ts) - tsOf(b.ts));
  const out: TimelineChatEvent[] = [];
  let i = 0;
  for (const ev of events) {
    // An event without a time keeps its place next to its neighbours.
    const t = tsOf((ev as { ts?: string | null }).ts);
    while (t !== Number.POSITIVE_INFINITY && i < cards.length && tsOf(cards[i].ts) < t) out.push(cards[i++]);
    out.push(ev);
  }
  while (i < cards.length) out.push(cards[i++]);
  return out;
}
