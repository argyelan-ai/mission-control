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
import type { RecordChat } from "@/lib/api";
import type { GroupSummary } from "@/lib/groupTypes";
import type { HeadRun } from "@/lib/heads";
import { openHeadQuestions } from "@/lib/inbox";
import { isAgentWorking } from "@/lib/agentStatus";
import { chatEntryHref, type ChatKind } from "@/lib/chatHistory";

/** waiting = the operator has to act · working = a turn runs · off = the
 *  agent's session is not running (not started, switched off, heartbeat
 *  gone) · idle = none of these. */
export type ChatListState = "waiting" | "working" | "off" | "idle";

/** What the session lists say about an agent's session beyond its status:
 *  host rows carry `session_running`, container rows `container_state`,
 *  both `last_seen_at` (heartbeat). Each is optional — a row without them
 *  makes no claim either way. */
export type SessionFacts = {
  session_running?: boolean;
  container_state?: string | null;
  last_seen_at?: string | null;
};

/** A heartbeat older than this means the session is gone — the same 90 s
 *  the backend uses for `session_running` and the comms directory's alive. */
export const SESSION_ALIVE_MS = 90_000;

/**
 * Is this agent's session not running (user test 21.11.2026: a switched-off
 * agent looked exactly like a running one in the picker)? True when the
 * host session is not running, the container is not running, or the last
 * heartbeat is missing or older than 90 s. A row that carries none of these
 * facts is not called off — nothing known, nothing claimed (K3).
 */
export function isAgentOff(agent: SessionFacts, now: Date = new Date()): boolean {
  if (agent.session_running === false) return true;
  if (agent.container_state != null && agent.container_state !== "running") return true;
  if (agent.last_seen_at === null) return true;
  if (typeof agent.last_seen_at === "string") {
    const seen = Date.parse(agent.last_seen_at);
    if (Number.isNaN(seen) || now.getTime() - seen > SESSION_ALIVE_MS) return true;
  }
  return false;
}

/** The picker's order: running agents first, those that are off below —
 *  each part keeps the order it came in. Only the picker sorts this way;
 *  the chat list stays newest first. */
export function runningFirst<T extends SessionFacts>(agents: readonly T[], now: Date = new Date()): T[] {
  const on = agents.filter((a) => !isAgentOff(a, now));
  const off = agents.filter((a) => isAgentOff(a, now));
  return [...on, ...off];
}

export type ChatListEntry = {
  key: string;
  kind: ChatKind;
  id: string;
  title: string;
  /** ISO time of the last thing that happened in this chat, if known. */
  at: string | null;
  state: ChatListState;
  href: string;
  /**
   * A further chat of an agent (chats slice S2, the record flag on): its
   * record id, and its number among the agent's chats (the agent row is
   * chat 1). The row names it "<agent> · chat <n>" while it carries no
   * title of its own.
   */
  chatId?: string;
  ordinal?: number;
};

export type ChatListSections = {
  waiting: ChatListEntry[];
  today: ChatListEntry[];
  earlier: ChatListEntry[];
};

export type ChatListInput = {
  agents: (Pick<
    Agent,
    "id" | "name" | "status" | "last_task_activity_at" | "last_chat_activity_at" | "updated_at"
  > &
    SessionFacts)[];
  groups: Pick<GroupSummary, "id" | "name" | "status" | "created_at" | "last_message">[];
  headRuns: HeadRun[];
  approvals: Pick<Approval, "agent_id" | "status">[];
  /** Chats of MC's chat record; empty while the record is off. */
  recordChats?: Pick<
    RecordChat,
    "id" | "title" | "harness" | "legacyAgentId" | "agentDefault" | "archived" | "lastActivityAt" | "createdAt"
  >[];
};

/** The one harness whose chat daemon holds further chats (S2: omp over ACP
 *  in its container — the backend refuses a chat id for any other). */
const FURTHER_CHAT_HARNESS = "omp";

/** The later of two ISO times; null only when both are missing. */
function later(a: string | null | undefined, b: string | null | undefined): string | null {
  if (!a) return b ?? null;
  if (!b) return a;
  return Date.parse(a) >= Date.parse(b) ? a : b;
}

export function buildChatList(input: ChatListInput, now: Date = new Date()): ChatListEntry[] {
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
      // section the phone did not have). The chat's own time first — an
      // old task must not date a chat held today (user test 21.11.2026).
      at: agent.last_chat_activity_at ?? agent.last_task_activity_at ?? null,
      state: pending.has(agent.id)
        ? "waiting"
        : isAgentOff(agent, now)
          ? "off"
          : isAgentWorking(agent.status)
            ? "working"
            : "idle",
      href: chatEntryHref("agent", agent.id),
    });
  }

  // Further chats of an agent (S2): one row each, next to the agent's own
  // row, numbered 2, 3, … by when they were created — never by the list's
  // order (the record sorts by activity, so writing in one chat would
  // renumber the others). The agent's default chat IS the agent row; an
  // archived chat, one of an agent not listed, or one of a harness whose
  // daemon holds no further chats (only omp does) shows nothing.
  const listed = new Map(
    input.agents.filter((a) => a.status !== "archived").map((a) => [a.id, a] as const),
  );
  const further = (input.recordChats ?? []).filter(
    (c) =>
      !c.agentDefault &&
      !c.archived &&
      c.harness === FURTHER_CHAT_HARNESS &&
      c.legacyAgentId &&
      listed.has(c.legacyAgentId),
  );
  const created = [...further].sort(
    (a, b) =>
      (a.createdAt ?? "\uffff").localeCompare(b.createdAt ?? "\uffff") || a.id.localeCompare(b.id),
  );
  const ordinalOf = new Map<string, number>();
  const counts = new Map<string, number>();
  for (const chat of created) {
    const n = (counts.get(chat.legacyAgentId as string) ?? 1) + 1;
    counts.set(chat.legacyAgentId as string, n);
    ordinalOf.set(chat.id, n);
  }
  for (const chat of further) {
    const agent = listed.get(chat.legacyAgentId as string)!;
    const n = ordinalOf.get(chat.id)!;
    const ownTitle = chat.title && chat.title !== agent.name ? chat.title : null;
    entries.push({
      key: `chat:${chat.id}`,
      kind: "agent",
      id: agent.id,
      title: ownTitle ?? agent.name,
      at: chat.lastActivityAt ?? null,
      // The agent's state is its own chat's; a further chat claims none.
      state: "idle",
      href: `${chatEntryHref("agent", agent.id)}&chat=${encodeURIComponent(chat.id)}`,
      chatId: chat.id,
      ordinal: ownTitle ? undefined : n,
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

// ── "New chat" draft: who the agent chip offers (concept "Neuer Chat", P1 + P4) ──

/** A host agent counts as alive for "New chat" while its last heartbeat is
 *  younger than a day — the 90 s session rule above would hide an agent that
 *  merely dozed; a test agent silent for days must not be offered. */
export const NEW_CHAT_HOST_ALIVE_MS = 24 * 60 * 60 * 1000;

/** The facts of a session row the chip needs. */
export type NewChatAgent = {
  id: string;
  name: string;
  status: string;
  agent_runtime?: string | null;
  container_state?: string | null;
  last_seen_at?: string | null;
  can_start_chat?: boolean | null;
};

export type NewChatChoices<T extends NewChatAgent = NewChatAgent> = {
  /** Alive agents that can hold a further chat — a send makes a new chat. */
  canStart: T[];
  /** Alive agents with only their own chat — a tap opens that chat. */
  onlyOne: T[];
};

/** Alive for "New chat": host = heartbeat under 24 h; container = running
 *  (status not offline when the row carries no container fact). Archived
 *  never. */
export function isAgentAliveForNewChat(agent: NewChatAgent, now: Date = new Date()): boolean {
  if (agent.status === "archived") return false;
  if (agent.agent_runtime === "host") {
    if (!agent.last_seen_at) return false;
    const seen = Date.parse(agent.last_seen_at);
    return !Number.isNaN(seen) && now.getTime() - seen < NEW_CHAT_HOST_ALIVE_MS;
  }
  if (agent.container_state != null) return agent.container_state === "running";
  return agent.status !== "offline";
}

/**
 * The chip's two sections, in the order the rows came in. `newChatsOn` is
 * false while the chat record is off or the viewer is no admin (only admins
 * may create chats): then nobody gets a new chat and every alive agent opens
 * its own. Host agents get no further chats in this round, whatever a row
 * claims.
 */
export function newChatChoices<T extends NewChatAgent>(
  agents: readonly T[],
  opts: { newChatsOn: boolean },
  now: Date = new Date(),
): NewChatChoices<T> {
  const alive = agents.filter((a) => isAgentAliveForNewChat(a, now));
  const can = (a: T) => opts.newChatsOn && a.can_start_chat === true && a.agent_runtime !== "host";
  return { canStart: alive.filter(can), onlyOne: alive.filter((a) => !can(a)) };
}

/** The agent the draft starts with: the last used one that can start a chat,
 *  else the first such; with none, the last used alive agent (its own chat),
 *  else the first; nobody alive → null. `recentAgentIds` newest first. */
export function defaultNewChatAgent<T extends NewChatAgent>(
  choices: NewChatChoices<T>,
  recentAgentIds: readonly string[],
): T | null {
  for (const list of [choices.canStart, choices.onlyOne]) {
    if (list.length === 0) continue;
    for (const id of recentAgentIds) {
      const hit = list.find((a) => a.id === id);
      if (hit) return hit;
    }
    return list[0];
  }
  return null;
}

/**
 * The first message of a new chat, handed from the draft to the real chat it
 * became: the draft sent it already; the chat that opens next shows it as its
 * own echo until the record confirms it, so the line never blinks out between
 * the two screens. Taken once. Key: the record chat id, or `agent:<id>` for
 * an agent's own chat.
 */
const handedMessages = new Map<string, string>();

export function handOverFirstMessage(key: string, text: string): void {
  handedMessages.set(key, text);
}

export function takeHandedMessage(key: string): string | null {
  const text = handedMessages.get(key) ?? null;
  handedMessages.delete(key);
  return text;
}
