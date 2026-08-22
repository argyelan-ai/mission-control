import { describe, it, expect } from "vitest";
import { matchRuns, isAgentSpawn, notificationsByTool } from "./agentRuns";
import type { ChatEvent, SubagentRun, ToolEvent } from "@/lib/chatTypes";

function spawn(
  toolUseId: string,
  opts: { name?: string; agentId?: string; description?: string } = {},
): ToolEvent {
  return {
    kind: "tool",
    uuid: `u-${toolUseId}`,
    ts: "2026-08-22T10:00:00Z",
    name: "Agent",
    title: "Agent: irgendwas",
    detail: {
      ...(opts.name ? { name: opts.name } : {}),
      ...(opts.description ? { description: opts.description } : {}),
    },
    toolUseId,
    result: opts.agentId
      ? `Spawned successfully.\nagent_id: ${opts.agentId}\nname: x`
      : null,
    status: "done",
    stats: null,
    sidechain: false,
  } as ToolEvent;
}

function run(
  runId: string,
  name: string | null,
  team = "session-abc",
  description: string | null = null,
): SubagentRun {
  return {
    runId, name, teamName: team, description,
    agentType: "worker", model: null, color: null,
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


  it("trifft ueber den Auftragstext, wenn Name und agent_id fehlen", () => {
    // Der praktisch wichtigste Rueckfall: Container-Agenten fahren teils noch
    // CLI 2.1.207, dort fehlt die agent_id-Zeile UND der Name im Steckbrief.
    // Live am 22.08.2026 an einem echten Lauf von Tester beobachtet — der
    // Auftragstext war das einzige gemeinsame Feld. Ueber 602 Spawns gemessen
    // hebt dieser Schluessel die Abdeckung von 50,2 % auf 95,5 %.
    const ev = spawn("t1", { description: "pruef-demo OS check" });
    const runs = [run("a1", null, "session-abc", "pruef-demo OS check")];

    expect(matchRuns([ev], runs).get("t1")).toBe(runs[0]);
  });

  it("ordnet NICHT zu, wenn derselbe Auftragstext mehrfach vorkommt", () => {
    // 16 der 602 gemessenen Spawns sind genau dieser Fall.
    const ev = spawn("t1", { description: "gleicher Auftrag" });
    const runs = [
      run("a1", null, "session-abc", "gleicher Auftrag"),
      run("a2", null, "session-abc", "gleicher Auftrag"),
    ];

    expect(matchRuns([ev], runs).has("t1")).toBe(false);
  });

  it("der Auftragstext geht dem Namen vor", () => {
    // Beide Rueckfaelle sind moeglich; der Text ist spezifischer.
    const ev = spawn("t1", { name: "pruefer", description: "der genaue Auftrag" });
    const runs = [
      run("a1", "pruefer", "session-abc", "ein anderer Auftrag"),
      run("a2", null, "session-abc", "der genaue Auftrag"),
    ];

    expect(matchRuns([ev], runs).get("t1")?.runId).toBe("a2");
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

describe("notificationsByTool", () => {
  function notice(toolUseId: string | null, status: string, uuid = "n1") {
    return {
      kind: "notification", uuid, ts: "2026-08-22T10:00:00Z",
      taskId: "t", toolUseId, status, summary: `etwas ${status}`,
    } as ChatEvent;
  }

  it("ordnet eine Meldung ihrem Werkzeugaufruf zu", () => {
    const m = notificationsByTool([notice("t1", "completed")]);
    expect(m.get("t1")?.status).toBe("completed");
  });

  it("laesst eine Meldung ohne Werkzeugaufruf aussen vor", () => {
    // 11 von 77 gemessenen Meldungen haben keine tool-use-id. Sie gehoeren
    // dann zu keinem Aufruf und bleiben eine eigene Zeile.
    expect(notificationsByTool([notice(null, "completed")]).size).toBe(0);
  });

  it("nimmt bei mehreren Meldungen die spaetere", () => {
    // Die CLI sagt in ihrer eigenen Meldung, dass derselbe Vorgang mehrfach
    // melden kann. Der spaetere Stand ist der wahre.
    const m = notificationsByTool([
      notice("t1", "completed", "n1"),
      notice("t1", "failed", "n2"),
    ]);
    expect(m.get("t1")?.status).toBe("failed");
  });

  it("ignoriert alles, was keine Meldung ist", () => {
    expect(notificationsByTool([spawn("t1", { name: "x" })]).size).toBe(0);
  });
});
