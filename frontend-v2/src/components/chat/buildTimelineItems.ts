/**
 * Timeline-Gruppierung — aus ChatView.tsx ausgezogen (PR #595 Nacharbeit).
 *
 * Warum ein eigenes Modul: ChatView rief `buildTimelineItems` bei jedem
 * Render auf (vor dem useMemo-Fix bei jedem Preview-Tick, alle 0.3 s). Als
 * Funktion innerhalb der Komponenten-Datei gab es keine Naht, an der ein
 * Test ansetzen konnte, um die Aufrufe zu zaehlen — der alte Probe-Test
 * bildete das Muster nur nach, statt den echten Code zu pruefen. Als
 * eigenes Modul kann ein Test die echte Funktion via Modul-Spy zaehlen,
 * waehrend ChatView rendert (siehe buildTimelineItems.seam-Abschnitt in
 * ChatView.test.tsx). Reines Umhaengen: gleiche Ein- und Ausgaben.
 */
import { isAgentSpawn } from "./agentRuns";
import type { ActivityEvent } from "./ToolGroup";
import type { TimelineChatEvent, ToolEvent } from "@/lib/chatTypes";

function isSidechain(ev: TimelineChatEvent): boolean {
  // CommandEvent carries no `sidechain` field (chatTypes.ts) — narrow safely
  // instead of assuming every union member has the property.
  return "sidechain" in ev && ev.sidechain === true;
}

function isActivity(ev: TimelineChatEvent): ev is ActivityEvent {
  return ev.kind === "tool" || ev.kind === "thinking";
}

export type TimelineItem =
  /** A message or command, or a run too short to be worth collapsing. */
  | { kind: "single"; event: TimelineChatEvent }
  /** A run of consecutive tool/thinking events → one ToolGroup chip. */
  | { kind: "activity"; events: ActivityEvent[] }
  /** A run of consecutive sidechain (subagent) events → one SubagentGroup. */
  | { kind: "sidechain"; events: TimelineChatEvent[] }
  /** Ein delegierter Auftrag (Werkzeug `Agent`) → eine eigene Karte. */
  | { kind: "agent"; event: ToolEvent };

/** Runs shorter than this render as plain rows: collapsing a single tool call
 *  behind "1 Befehl ausgeführt" would hide its title (the useful part) and
 *  cost a tap to get it back. Two or more is where the wall starts. */
export const ACTIVITY_GROUP_MIN_SIZE = 2;

/**
 * Turns the flat event list into the timeline's render items.
 *
 * Two independent runs are accumulated: sidechain events (subagent turns,
 * unchanged behavior) and top-level tool/thinking events (the new activity
 * groups). Any other event — an assistant text message, a user message, a
 * slash command — closes both runs, which is exactly the group boundary the
 * reference contract asks for: a group covers one working stretch between two
 * things a human said or read.
 */
export function buildTimelineItems(events: TimelineChatEvent[]): TimelineItem[] {
  const out: TimelineItem[] = [];
  let sidechainRun: TimelineChatEvent[] = [];
  let activityRun: ActivityEvent[] = [];

  function flushSidechain() {
    if (sidechainRun.length > 0) {
      out.push({ kind: "sidechain", events: sidechainRun });
      sidechainRun = [];
    }
  }

  function flushActivity() {
    if (activityRun.length === 0) return;
    if (activityRun.length >= ACTIVITY_GROUP_MIN_SIZE) {
      out.push({ kind: "activity", events: activityRun });
    } else {
      for (const ev of activityRun) out.push({ kind: "single", event: ev });
    }
    activityRun = [];
  }

  const absorbiert = new Set(
    events.filter(isAgentSpawn).map((ev) => ev.toolUseId).filter(Boolean) as string[],
  );

  for (const ev of events) {
    if (ev.kind === "notification" && ev.toolUseId && absorbiert.has(ev.toolUseId)) {
      /* Gehoert zu einer Karte — dort wird sie gezeigt. Zweimal dasselbe
         nebeneinander war genau das Rauschen, das hier weg soll. */
      continue;
    }
    if (isSidechain(ev)) {
      flushActivity();
      sidechainRun.push(ev);
      continue;
    }
    flushSidechain();
    if (isAgentSpawn(ev)) {
      /* Muss VOR der Aktivitaets-Sammlung stehen: sonst verschwindet der
         Auftrag als anonymes "+1 Tool" in einer Werkzeug-Gruppe, weil er in
         der Praxis fast immer neben Bash/Read steht. */
      flushActivity();
      out.push({ kind: "agent", event: ev });
      continue;
    }
    if (isActivity(ev)) {
      activityRun.push(ev);
      continue;
    }
    flushActivity();
    out.push({ kind: "single", event: ev });
  }

  flushActivity();
  flushSidechain();
  return out;
}
