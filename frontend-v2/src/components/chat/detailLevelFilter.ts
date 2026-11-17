/**
 * Which chat events a detail level shows — moved out of ChatView so the
 * list-wide level "answer" (it needs to know which reply is a turn's LAST)
 * has a seam of its own to test.
 *
 * Always visible, in every level: a question to the operator (elicitation
 * card) and an error card. The approval prompt is not a timeline event — it
 * stands below the conversation and no level filters it.
 */
import { isAgentSpawn } from "./agentRuns";
import type { DetailLevel } from "./chatOptions";
import type { MessageEvent, TimelineChatEvent } from "@/lib/chatTypes";

function isErrorCard(ev: TimelineChatEvent): boolean {
  return ev.kind === "message" && ev.role === "teammate" && ev.source?.kind === "error";
}

/** Something the operator (or MC on their behalf) put into the chat — the
 *  start of a turn: their own message, a slash command, a delivered task or
 *  inbox message. */
function startsTurn(ev: TimelineChatEvent): boolean {
  if (ev.kind === "command") return true;
  if (ev.kind !== "message" || ev.sidechain) return false;
  if (ev.role === "user") return true;
  return ev.role === "teammate" && (ev.source?.kind === "task" || ev.source?.kind === "inbox");
}

function isAgentReply(ev: TimelineChatEvent): ev is MessageEvent {
  return ev.kind === "message" && ev.role === "assistant" && !ev.sidechain;
}

function isVisibleAtLevel(ev: TimelineChatEvent, level: DetailLevel): boolean {
  if (level !== "compact") return true;
  /* Ein delegierter Auftrag ist Gespraechsstruktur, kein Werkzeug-Rauschen:
     er sagt, WER hier gerade woran arbeitet. Ohne diese Ausnahme verschwaende
     die Karte in "Kompakt" spurlos — samt dem einzigen Zugang zum Protokoll
     des Subagenten. */
  if (isAgentSpawn(ev)) return true;
  // Kompakt: tool/thinking rows are noise for a quick skim — hide entirely
  // rather than collapse (that's what Normal is for). Since no tool/thinking
  // event survives this filter, `Kompakt` also produces no activity groups —
  // the level's semantics are unchanged by the grouping layer.
  return ev.kind === "message" || ev.kind === "command" || ev.kind === "elicitation";
}

/** "Answer only": per turn the operator's input and the agent's last reply.
 *  While a turn runs, its newest reply so far stands in for the last one. */
function answerOnly(events: TimelineChatEvent[]): TimelineChatEvent[] {
  const lastReply = new Set<TimelineChatEvent>();
  let candidate: TimelineChatEvent | null = null;
  for (const ev of events) {
    if (startsTurn(ev)) {
      if (candidate) lastReply.add(candidate);
      candidate = null;
    } else if (isAgentReply(ev)) {
      candidate = ev;
    }
  }
  if (candidate) lastReply.add(candidate);
  return events.filter(
    (ev) => startsTurn(ev) || lastReply.has(ev) || ev.kind === "elicitation" || isErrorCard(ev),
  );
}

export function filterByDetailLevel(events: TimelineChatEvent[], level: DetailLevel): TimelineChatEvent[] {
  if (level === "answer") return answerOnly(events);
  return events.filter((ev) => isVisibleAtLevel(ev, level));
}
