/**
 * The chat list of the new shell (ADR-102 "chat first", operator decision
 * 2026-11-18: variant C with the drawer from variant A).
 *
 * One list feeds both places it shows: the phone drawer (sidebar icon in the
 * chat header) and the always-visible left column on the desktop. Sections:
 *
 *   Waiting for you — a chat with an open approval, a group waiting at its
 *                     gate, a head that stopped with a question
 *   Today / Earlier — every other chat, newest first
 *
 * A chat is what MC has today: an agent session, an active group room, or a
 * head that waits on the operator. "Chats instead of fixed agents" (ADR-102
 * principle 2) changes the source later, not this shape.
 *
 * Pure: no React, no fetching — `components/layout/useChatList.ts` loads.
 */
import type { Agent, Approval } from "@/lib/types";
import type { GroupSummary } from "@/lib/groupTypes";
import type { HeadRun } from "@/lib/heads";
import { openHeadQuestions } from "@/lib/inbox";
import { isAgentWorking } from "@/lib/agentStatus";
import { chatEntryHref, type ChatKind } from "@/lib/chatHistory";

/** waiting = the operator has to act · working = a turn runs · idle = neither. */
export type ChatListState = "waiting" | "working" | "idle";

export type ChatListEntry = {
  key: string;
  kind: ChatKind;
  id: string;
  title: string;
  /** ISO time of the last thing that happened in this chat, if known. */
  at: string | null;
  state: ChatListState;
  href: string;
};

export type ChatListSections = {
  waiting: ChatListEntry[];
  today: ChatListEntry[];
  earlier: ChatListEntry[];
};

export type ChatListInput = {
  agents: Pick<Agent, "id" | "name" | "status" | "last_task_activity_at" | "updated_at">[];
  groups: Pick<GroupSummary, "id" | "name" | "status" | "created_at" | "last_message">[];
  headRuns: HeadRun[];
  approvals: Pick<Approval, "agent_id" | "status">[];
};

/** The later of two ISO times; null only when both are missing. */
function later(a: string | null | undefined, b: string | null | undefined): string | null {
  if (!a) return b ?? null;
  if (!b) return a;
  return Date.parse(a) >= Date.parse(b) ? a : b;
}

export function buildChatList(input: ChatListInput): ChatListEntry[] {
  const pending = new Set(input.approvals.filter((a) => a.status === "pending").map((a) => a.agent_id));
  const entries: ChatListEntry[] = [];

  for (const agent of input.agents) {
    if (agent.status === "archived") continue;
    entries.push({
      key: `agent:${agent.id}`,
      kind: "agent",
      id: agent.id,
      title: agent.name,
      // Server facts only, so every device sorts alike (operator pictures
      // 18.11.2026: a per-device "opened at" gave the desktop a Today
      // section the phone did not have).
      at: agent.last_task_activity_at ?? null,
      state: pending.has(agent.id) ? "waiting" : isAgentWorking(agent.status) ? "working" : "idle",
      href: chatEntryHref("agent", agent.id),
    });
  }

  for (const group of input.groups) {
    entries.push({
      key: `group:${group.id}`,
      kind: "group",
      id: group.id,
      title: group.name,
      at: later(group.last_message?.created_at, group.created_at),
      state: group.status === "waiting_gate" ? "waiting" : group.status === "running" ? "working" : "idle",
      href: chatEntryHref("group", group.id),
    });
  }

  // Heads only while they wait on an answer — a finished run is history, it
  // lives on the task, not in the chat list.
  for (const run of openHeadQuestions(input.headRuns)) {
    entries.push({
      key: `head:${run.run_id}`,
      kind: "head",
      id: run.run_id,
      title: run.title ?? run.run_id,
      at: run.created_at,
      state: "waiting",
      href: chatEntryHref("head", run.run_id),
    });
  }

  return entries.sort((a, b) => (b.at ? Date.parse(b.at) : 0) - (a.at ? Date.parse(a.at) : 0));
}

function sameDay(a: Date, b: Date): boolean {
  return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
}

/** Split into the drawer's three sections; order inside each stays newest first. */
export function sectionChats(entries: ChatListEntry[], now: Date = new Date()): ChatListSections {
  const out: ChatListSections = { waiting: [], today: [], earlier: [] };
  for (const e of entries) {
    if (e.state === "waiting") out.waiting.push(e);
    else if (e.at && sameDay(new Date(e.at), now)) out.today.push(e);
    else out.earlier.push(e);
  }
  return out;
}

/** The quiet time at the end of a row: "14:02" today, "14.11." / "11/14" before. */
export function chatRowTime(at: string | null, locale: string, now: Date = new Date()): string | null {
  if (!at) return null;
  const d = new Date(at);
  if (Number.isNaN(d.getTime())) return null;
  if (sameDay(d, now)) {
    return d.toLocaleTimeString(locale === "de" ? "de-CH" : "en-GB", { hour: "2-digit", minute: "2-digit" });
  }
  return locale === "de"
    ? `${d.getDate()}.${d.getMonth() + 1}.`
    : d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
}
