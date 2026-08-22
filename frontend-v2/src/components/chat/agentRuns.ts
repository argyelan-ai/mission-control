/**
 * Welcher Agent-Aufruf im Verlauf gehoert zu welchem Subagenten-Lauf?
 *
 * Der Aufruf steht im Hauptstrom (`tool`-Ereignis, Werkzeug `Agent`), der
 * Verlauf des Subagenten in einer eigenen Datei daneben. Verbunden sind die
 * beiden nur ueber das, was die CLI ins ERGEBNIS des Aufrufs schreibt:
 *
 *     agent_id: cc-transcript-research@session-6f861be5
 *               ^^^^^^^^^^^^^^^^^^^^^ ^^^^^^^^^^^^^^^^^
 *               meta.name             meta.teamName
 *
 * Beides zusammen ist ein EXAKTER Schluessel. Live gemessen ueber 91
 * Sitzungen und 601 Spawns (22.08.2026): wo die Zeile lesbar ist, trifft sie
 * in 97,1 % der Faelle genau einen Steckbrief — und die Fehltreffer sind
 * Spawns, deren Datei gar nicht existiert, keine Fehlzuordnungen.
 *
 * Lesbar ist sie allerdings nur in 51,7 % der Spawns, mit klarem Trend nach
 * oben bei neueren CLI-Staenden (2.1.233 und 2.1.235: 100 %). Fuer den Rest
 * gibt es den Rueckfall ueber `input.name` — aber NUR, wenn er eindeutig ist.
 *
 * Die Leitplanke dahinter: **falsch zuordnen ist schlimmer als nicht
 * zuordnen.** Eine Karte, die den Steckbrief eines fremden Laufs zeigt, ist
 * eine Luege; eine Karte ohne Steckbrief ist bloss karg. Darum wird ein Lauf
 * hoechstens EINMAL vergeben, und ein mehrdeutiger Rueckfall gar nicht.
 *
 * Dass es mehr Laeufe als Aufrufe gibt, ist der NORMALFALL, kein Fehler: der
 * Ordner enthaelt auch die Laeufe von Subagenten, die ihrerseits Subagenten
 * gestartet haben (`spawnDepth >= 1` in 359 von 754 Steckbriefen).
 */

import type { ChatEvent, SubagentRun, ToolEvent } from "@/lib/chatTypes";

/** `agent_id: <name>@<teamName>` aus dem Ergebnis des Agent-Aufrufs.
 *  Der Text drumherum wird NIE angezeigt — er traegt Spawn-Metadaten und
 *  bezeichnet sich selbst als nicht zitierfaehig. Hier dient er allein als
 *  Schluessel. */
const AGENT_ID_RE = /agent_id:\s*([A-Za-z0-9_.-]+)@(session-[A-Za-z0-9]+)/;

export function isAgentSpawn(ev: ChatEvent): ev is ToolEvent {
  return ev.kind === "tool" && (ev.name === "Agent" || ev.name === "Task");
}

function readAgentId(ev: ToolEvent): { name: string; team: string } | null {
  const raw = typeof ev.result === "string" ? ev.result : JSON.stringify(ev.result ?? "");
  const m = AGENT_ID_RE.exec(raw);
  return m ? { name: m[1], team: m[2] } : null;
}

function spawnName(ev: ToolEvent): string | null {
  const detail = ev.detail as Record<string, unknown> | null | undefined;
  const name = detail?.name;
  return typeof name === "string" && name ? name : null;
}

/**
 * Ordnet jedem Agent-Aufruf seinen Lauf zu — geschluesselt auf `toolUseId`.
 * Aufrufe ohne sicheren Treffer fehlen in der Abbildung; die Karte faellt
 * dann auf das zurueck, was im Aufruf selbst steht.
 */
export function matchRuns(
  events: readonly ChatEvent[],
  runs: readonly SubagentRun[],
): Map<string, SubagentRun> {
  const out = new Map<string, SubagentRun>();
  if (!runs.length) return out;

  const vergeben = new Set<string>();
  const spawns = events.filter(isAgentSpawn).filter((ev) => ev.toolUseId);

  // Durchgang 1 — exakter Schluessel. Laeuft zuerst, damit er nie einen Lauf
  // an den unsicheren Rueckfall verliert.
  for (const ev of spawns) {
    const id = readAgentId(ev);
    if (!id) continue;
    const treffer = runs.find(
      (r) => !vergeben.has(r.runId) && r.name === id.name && r.teamName === id.team,
    );
    if (treffer) {
      vergeben.add(treffer.runId);
      out.set(ev.toolUseId!, treffer);
    }
  }

  // Durchgang 2 — Rueckfall ueber den Namen aus dem Aufruf, und nur bei
  // Eindeutigkeit. Bleiben zwei gleichnamige Laeufe uebrig, wird geraten —
  // also gar nicht zugeordnet.
  for (const ev of spawns) {
    if (out.has(ev.toolUseId!)) continue;
    const name = spawnName(ev);
    if (!name) continue;
    const offen = runs.filter((r) => !vergeben.has(r.runId) && r.name === name);
    if (offen.length !== 1) continue;
    vergeben.add(offen[0].runId);
    out.set(ev.toolUseId!, offen[0]);
  }

  return out;
}
