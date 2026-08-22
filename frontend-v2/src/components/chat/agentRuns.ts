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
 * Lesbar ist sie allerdings nur in gut der Haelfte der Spawns, mit klarem
 * Trend nach oben bei neueren CLI-Staenden (2.1.233/235: 100 %) — die Flotte
 * faehrt aber teils noch 2.1.207, wo sie fehlt. Darum zwei Rueckfaelle, beide
 * nur bei EINDEUTIGKEIT:
 *
 *   2. `input.description` == `meta.description`. Der Auftragstext, den der
 *      Aufrufer vergibt, landet unveraendert im Steckbrief. Live gemessen
 *      ueber 602 Spawns: hebt die Abdeckung von 50,2 % auf 95,5 %; 16 Faelle
 *      bleiben mehrdeutig (mehrfach derselbe Auftragstext) und werden
 *      uebersprungen, 11 haben ueberhaupt keine Datei.
 *   3. `input.name` == `meta.name`, fuer die wenigen Faelle mit Namen aber
 *      ohne verwertbaren Auftragstext.
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

function detailText(ev: ToolEvent, key: "name" | "description"): string | null {
  const detail = ev.detail as Record<string, unknown> | null | undefined;
  const value = detail?.[key];
  return typeof value === "string" && value.trim() ? value : null;
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

  /* Durchgang 2 und 3 — Rueckfaelle, jeweils NUR bei Eindeutigkeit. Bleiben
     zwei Kandidaten uebrig, wird geraten, also gar nicht zugeordnet. Erst der
     Auftragstext, dann der Name: der Text ist spezifischer und in der Praxis
     deutlich haeufiger vorhanden. */
  for (const key of ["description", "name"] as const) {
    for (const ev of spawns) {
      if (out.has(ev.toolUseId!)) continue;
      const wert = detailText(ev, key);
      if (!wert) continue;
      const offen = runs.filter(
        (r) => !vergeben.has(r.runId) && (key === "description" ? r.description : r.name) === wert,
      );
      if (offen.length !== 1) continue;
      vergeben.add(offen[0].runId);
      out.set(ev.toolUseId!, offen[0]);
    }
  }

  return out;
}
