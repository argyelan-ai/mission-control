import { describe, it, expect } from "vitest";
import { matchRuns, isAgentSpawn } from "./agentRuns";
import type { SubagentRun, ToolEvent } from "@/lib/chatTypes";

function spawn(
  toolUseId: string,
  opts: { name?: string; agentId?: string } = {},
): ToolEvent {
  return {
    kind: "tool",
    uuid: `u-${toolUseId}`,
    ts: "2026-08-22T10:00:00Z",
    name: "Agent",
    title: "Agent: irgendwas",
    detail: opts.name ? { name: opts.name } : {},
    toolUseId,
    result: opts.agentId
      ? `Spawned successfully.\nagent_id: ${opts.agentId}\nname: x`
      : null,
    status: "done",
    stats: null,
    sidechain: false,
  } as ToolEvent;
}

function run(runId: string, name: string | null, team = "session-abc"): SubagentRun {
  return {
    runId, name, teamName: team,
    agentType: "worker", description: null, model: null, color: null,
    startedAt: "2026-08-22T10:00:00Z",
  };
}

describe("matchRuns", () => {
  it("trifft ueber den exakten Schluessel aus dem Ergebnis", () => {
    // Die CLI schreibt `agent_id: <name>@<teamName>` ins Ergebnis des
    // Aufrufs — beides zusammen steht so auch im Steckbrief. Gemessen an 91
    // Sitzungen: wo die Zeile da ist, trifft sie zu 97,1 %.
    const ev = spawn("t1", { agentId: "pruefer@session-abc" });
    const runs = [run("a1", "pruefer")];

    expect(matchRuns([ev], runs).get("t1")).toBe(runs[0]);
  });

  it("faellt auf den Namen aus dem Aufruf zurueck, wenn das Ergebnis nichts hergibt", () => {
    // In knapp der Haelfte der Spawns fehlt die agent_id-Zeile.
    const ev = spawn("t1", { name: "pruefer" });
    const runs = [run("a1", "pruefer")];

    expect(matchRuns([ev], runs).get("t1")).toBe(runs[0]);
  });

  it("ordnet NICHT zu, wenn der Rueckfall mehrdeutig waere", () => {
    // Zwei gleichnamige Laeufe, ein Aufruf ohne exakten Schluessel: jede Wahl
    // waere geraten. Eine Karte mit fremdem Steckbrief ist schlimmer als eine
    // ohne.
    const ev = spawn("t1", { name: "pruefer" });
    const runs = [run("a1", "pruefer"), run("a2", "pruefer")];

    expect(matchRuns([ev], runs).has("t1")).toBe(false);
  });

  it("loest dieselbe Mehrdeutigkeit auf, sobald der exakte Schluessel da ist", () => {
    // Gegenprobe zum Test darueber: mit agent_id ist auch bei gleichem Namen
    // klar, welcher Lauf gemeint ist — die Mannschaft unterscheidet sie.
    const ev = spawn("t1", { agentId: "pruefer@session-zwei", name: "pruefer" });
    const runs = [run("a1", "pruefer", "session-eins"), run("a2", "pruefer", "session-zwei")];

    expect(matchRuns([ev], runs).get("t1")?.runId).toBe("a2");
  });

  it("vergibt einen Lauf hoechstens einmal", () => {
    // Zwei Aufrufe, beide ohne exakten Schluessel, ein einziger Lauf: der
    // erste bekommt ihn, der zweite bleibt leer — statt dass beide Karten
    // denselben Verlauf zeigen.
    const evs = [spawn("t1", { name: "pruefer" }), spawn("t2", { name: "pruefer" })];
    const runs = [run("a1", "pruefer")];

    const m = matchRuns(evs, runs);
    expect(m.get("t1")).toBe(runs[0]);
    expect(m.has("t2")).toBe(false);
  });

  it("der exakte Schluessel geht dem Rueckfall vor", () => {
    // Sonst schnappte der Rueckfall des ersten Aufrufs den Lauf weg, den der
    // zweite nachweislich besitzt.
    const evs = [spawn("t1", { name: "pruefer" }), spawn("t2", { agentId: "pruefer@session-abc" })];
    const runs = [run("a1", "pruefer")];

    const m = matchRuns(evs, runs);
    expect(m.get("t2")).toBe(runs[0]);
    expect(m.has("t1")).toBe(false);
  });

  it("mehr Laeufe als Aufrufe ist der Normalfall, kein Fehler", () => {
    // Der Ordner enthaelt auch die Laeufe von Subagenten, die selbst welche
    // gestartet haben (spawnDepth >= 1 in 359 von 754 Steckbriefen).
    const ev = spawn("t1", { agentId: "pruefer@session-abc" });
    const runs = [run("a1", "pruefer"), run("a2", "enkel"), run("a3", "urenkel")];

    const m = matchRuns([ev], runs);
    expect(m.size).toBe(1);
    expect(m.get("t1")?.runId).toBe("a1");
  });

  it("ohne Laeufe und ohne Aufrufe passiert schlicht nichts", () => {
    expect(matchRuns([spawn("t1", { name: "x" })], []).size).toBe(0);
    expect(matchRuns([], [run("a1", "x")]).size).toBe(0);
  });

  it("erkennt Agent und Task als Spawn, sonst nichts", () => {
    // Das heutige CLI nennt es 612x "Agent" und 0x "Task" — "Task" bleibt
    // trotzdem drin, es ist der aeltere Name desselben Werkzeugs.
    expect(isAgentSpawn(spawn("t1"))).toBe(true);
    expect(isAgentSpawn({ ...spawn("t1"), name: "Task" } as ToolEvent)).toBe(true);
    expect(isAgentSpawn({ ...spawn("t1"), name: "Bash" } as ToolEvent)).toBe(false);
  });
});
