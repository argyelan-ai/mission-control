import { describe, it, expect } from "vitest";
import { resolveAliveness } from "@/lib/chatTypes";
import {
  isSessionClearingCommand,
  retireEchoesAnsweredByRollover,
  chatReducer,
  markEchoRetried,
  markEchoStarting,
  createInitialChatState,
  markUnconfirmedEchoes,
  reconcilePendingEchoes,
  takeQueuedEchoes,
  withdrawPendingEcho,
  ECHO_CONFIRM_TIMEOUT_MS,
  MAX_CHAT_EVENTS,
  seedSequence,
  type PendingEcho,
} from "./useChatStream";
import type {
  ChatEvent,
  CommandEvent,
  MessageEvent,
  PreviewEvent,
  SessionChangedEvent,
  StateEvent,
  ThinkingEvent,
  ToolEvent,
  UsageEvent,
} from "@/lib/chatTypes";

// Tests the exported pure `chatReducer(state, event)` — not the hook itself
// (per task brief: the hook wires TanStack Query + useSSE around it, which
// needs a QueryClientProvider + EventSource mocking to exercise properly and
// is out of scope here).

function msg(uuid: string, text = "hi", role: MessageEvent["role"] = "user"): MessageEvent {
  return { kind: "message", uuid, ts: "2026-08-15T00:00:00Z", role, text, model: null, sidechain: false };
}

function tool(
  uuid: string,
  toolUseId: string | null,
  overrides: Partial<ToolEvent> = {},
): ToolEvent {
  return {
    kind: "tool",
    uuid,
    ts: "2026-08-15T00:00:00Z",
    name: "Read",
    title: "Read foo.py",
    detail: { file_path: "/foo.py" },
    toolUseId,
    result: null,
    status: "done",
    stats: null,
    sidechain: false,
    ...overrides,
  };
}

describe("chatReducer", () => {
  it("appends a message event", () => {
    const state = chatReducer(createInitialChatState(), msg("u1"));
    expect(state.events).toHaveLength(1);
    expect(state.events[0]).toMatchObject({ kind: "message", uuid: "u1" });
  });

  it("ignores a duplicate uuid for non-tool events", () => {
    let state = chatReducer(createInitialChatState(), msg("u1", "first"));
    state = chatReducer(state, msg("u1", "second"));
    expect(state.events).toHaveLength(1);
    expect((state.events[0] as MessageEvent).text).toBe("first");
  });

  it("replaces a tool event with the same toolUseId (result merge)", () => {
    let state = chatReducer(createInitialChatState(), tool("u1", "tu-1"));
    expect(state.events).toHaveLength(1);
    expect((state.events[0] as ToolEvent).result).toBeNull();

    state = chatReducer(
      state,
      tool("u1", "tu-1", { result: "file contents", status: "done" }),
    );
    expect(state.events).toHaveLength(1);
    expect((state.events[0] as ToolEvent).result).toBe("file contents");
  });

  it("replaces and marks error status on a failed tool result", () => {
    let state = chatReducer(createInitialChatState(), tool("u1", "tu-1"));
    state = chatReducer(state, tool("u1", "tu-1", { result: "boom", status: "error" }));
    expect(state.events).toHaveLength(1);
    expect((state.events[0] as ToolEvent).status).toBe("error");
  });

  it("keeps two parallel tool calls from the same assistant turn distinct (same uuid, different toolUseId)", () => {
    // Claude Code stamps every block in one assistant entry with the same
    // top-level uuid — an entry with two tool_use blocks produces two
    // ToolEvents sharing that uuid but distinct toolUseIds. A naive
    // dedup-by-uuid-alone reducer would collapse the second call into the
    // first; this is the regression guard for that.
    let state = chatReducer(createInitialChatState(), tool("turn-1", "tu-a", { title: "Read a.py" }));
    state = chatReducer(state, tool("turn-1", "tu-b", { title: "Read b.py" }));
    expect(state.events).toHaveLength(2);
    expect((state.events[0] as ToolEvent).title).toBe("Read a.py");
    expect((state.events[1] as ToolEvent).title).toBe("Read b.py");
  });

  it("updates the state slot without touching the events list", () => {
    const stateEv: StateEvent = { kind: "state", status: "working", prompt: null };
    const state = chatReducer(createInitialChatState(), stateEv);
    expect(state.events).toHaveLength(0);
    expect(state.state).toEqual(stateEv);
  });

  it("updates the usage slot without touching the events list", () => {
    const usageEv: UsageEvent = {
      kind: "usage",
      uuid: "u1",
      ts: "2026-08-15T00:00:00Z",
      inputTokens: 100,
      outputTokens: 20,
      model: "claude-sonnet-5",
      effort: null,
    };
    const state = chatReducer(createInitialChatState(), usageEv);
    expect(state.events).toHaveLength(0);
    expect(state.usage).toEqual(usageEv);
  });

  it("clears events and bumps sessionChangedAt on session_changed", () => {
    let state = chatReducer(createInitialChatState(), msg("u1"));
    state = chatReducer(state, msg("u2"));
    expect(state.events).toHaveLength(2);

    const sessionChanged: SessionChangedEvent = { kind: "session_changed" };
    state = chatReducer(state, sessionChanged);
    expect(state.events).toHaveLength(0);
    expect(state.sessionChangedAt).toBe(1);

    // A second rollover keeps incrementing so the hook can detect it even
    // if events were re-seeded to the same length in between.
    state = chatReducer(state, sessionChanged);
    expect(state.sessionChangedAt).toBe(2);
  });

  it("behaelt den Zustand ueber einen Sitzungswechsel, leert aber den Verbrauch", () => {
    // Zwei Faecher, zwei Antworten — der Name des Tests versprach frueher
    // beides und pruefte nur eines.
    //
    // ZUSTAND bleibt: er beschreibt den Agenten und sein Terminal. Ein
    // Rollover wechselt die Transkript-Datei, nicht den Agenten; ihn zu
    // leeren erzeugte nur ein Flackern nach "Status unklar".
    //
    // VERBRAUCH geht: die Tokens gehoerten zum geloeschten Gespraech. Blieben
    // sie stehen, zeigte der Kontext-Ring nach /clear weiter z. B. 87 %, bis
    // der Agent das naechste Mal antwortet — bei einem ruhenden Agenten
    // beliebig lange.
    let state = chatReducer(createInitialChatState(), {
      kind: "state",
      status: "idle",
      prompt: null,
    } as StateEvent);
    state = chatReducer(state, {
      kind: "usage", uuid: "x1", ts: "2026-08-21T00:00:00Z",
      inputTokens: 1000, outputTokens: 200, model: "opus", contextPct: 87,
    } as unknown as UsageEvent);
    state = chatReducer(state, msg("u1"));
    state = chatReducer(state, { kind: "session_changed" } as SessionChangedEvent);

    expect(state.state).toEqual({ kind: "state", status: "idle", prompt: null });
    expect(state.usage).toBeNull();
    expect(state.events).toEqual([]);
  });

  it("handles thinking and command events like other timeline kinds", () => {
    const thinking: ThinkingEvent = {
      kind: "thinking",
      uuid: "t1",
      ts: "2026-08-15T00:00:00Z",
      text: "pondering",
      sidechain: false,
    };
    const command: CommandEvent = {
      kind: "command",
      uuid: "c1",
      ts: "2026-08-15T00:00:00Z",
      command: "/compact",
    };
    let state = chatReducer(createInitialChatState(), thinking);
    state = chatReducer(state, command);
    expect(state.events).toHaveLength(2);
    expect(state.events.map((e) => e.kind)).toEqual(["thinking", "command"]);
  });

  it("caps events at MAX_CHAT_EVENTS, dropping the oldest", () => {
    let state = createInitialChatState();
    const total = MAX_CHAT_EVENTS + 10;
    for (let i = 0; i < total; i++) {
      state = chatReducer(state, msg(`u${i}`));
    }
    expect(state.events).toHaveLength(MAX_CHAT_EVENTS);
    // The oldest 10 were dropped — first surviving event is u10, last is u(total-1).
    expect((state.events[0] as MessageEvent).uuid).toBe("u10");
    expect((state.events[state.events.length - 1] as MessageEvent).uuid).toBe(`u${total - 1}`);
  });

  it("keeps dedup/replace working correctly after an eviction reindex", () => {
    let state = createInitialChatState();
    const total = MAX_CHAT_EVENTS + 5;
    for (let i = 0; i < total; i++) {
      state = chatReducer(state, tool(`u${i}`, `tu-${i}`));
    }
    // Replace a still-present tool event (u(total-1), the most recent) after
    // eviction has rebuilt the index — must still resolve by toolUseId.
    state = chatReducer(
      state,
      tool(`u${total - 1}`, `tu-${total - 1}`, { result: "done!", status: "done" }),
    );
    expect(state.events).toHaveLength(MAX_CHAT_EVENTS);
    const last = state.events[state.events.length - 1] as ToolEvent;
    expect(last.result).toBe("done!");
  });

  it("ignores unknown event kinds without throwing", () => {
    const weird = { kind: "_tool_result" } as unknown as ChatEvent;
    const before = createInitialChatState();
    const after = chatReducer(before, weird);
    expect(after).toBe(before);
  });
});


// ── Optimistic echo rules ────────────────────────────────────────────────────
// The three decisions that decide whether the operator ever sees a duplicated
// or a lying bubble. Pure, so they are tested without React.

function echo(overrides: Partial<PendingEcho> = {}): PendingEcho {
  return { id: "e1", text: "hallo", sentAt: 1_000, status: "pending", ...overrides };
}


describe("Live-Vorschau (preview) — eigenes Fach, nie Zeitachse", () => {
  const preview = (text: string): PreviewEvent =>
    ({ kind: "preview", uuid: null, ts: "2026-08-31T00:00:00Z", text, source: "pane" });

  it("legt die Vorschau ins eigene Fach und laesst die Zeitachse in Ruhe", () => {
    const state = chatReducer(createInitialChatState(), preview("Ich schaue mir die Datei an"));
    expect(state.events).toHaveLength(0);
    expect(state.preview?.text).toBe("Ich schaue mir die Datei an");
  });

  it("ersetzt eine aeltere Vorschau durch die neuere", () => {
    let state = chatReducer(createInitialChatState(), preview("erst"));
    state = chatReducer(state, preview("dann"));
    expect(state.preview?.text).toBe("dann");
  });

  it("wird von der echten Antwort abgeloest", () => {
    let state = chatReducer(createInitialChatState(), preview("laeuft…"));
    state = chatReducer(state, msg("a1", "Fertig.", "assistant"));
    expect(state.preview).toBeNull();
    expect(state.events).toHaveLength(1);
  });

  it("bleibt stehen, wenn nur der Operator etwas schreibt", () => {
    let state = chatReducer(createInitialChatState(), preview("laeuft…"));
    state = chatReducer(state, msg("u1", "und weiter?", "user"));
    expect(state.preview?.text).toBe("laeuft…");
  });

  it("verfaellt, sobald der Agent ruht — eine Waise ohne Antwort ist keine Antwort", () => {
    let state = chatReducer(createInitialChatState(), preview("laeuft…"));
    state = chatReducer(state, { kind: "state", status: "working", prompt: null });
    expect(state.preview?.text).toBe("laeuft…");
    state = chatReducer(state, { kind: "state", status: "idle", prompt: null });
    expect(state.preview).toBeNull();
  });

  it("verfaellt beim Sitzungswechsel", () => {
    let state = chatReducer(createInitialChatState(), preview("laeuft…"));
    state = chatReducer(state, { kind: "session_changed" });
    expect(state.preview).toBeNull();
  });
});

describe("gemischte Bloecke unter einer uuid (omp)", () => {
  it("behaelt Denken UND Antwort, wenn beide dieselbe Eintrags-uuid tragen", () => {
    // LIVE gemessen an dem echten omp-Transkript eines Agenten (21.08.2026): 10 von 25
    // Eintraegen tragen `thinking` UND `message` unter derselben uuid — der
    // omp-Adapter schreibt beide Bloecke in EINE Zeile. Mit der uuid allein
    // als Schluessel galt die Antwort als Dublette des Denkens und wurde
    // verworfen: der Agent dachte im Chat sichtbar nach und sagte nie etwas.
    //
    // Bei Claude Code faellt das nicht auf (eine Zeile je Block), der
    // Backend-Parser unterstuetzt gemischte Bloecke aber ausdruecklich —
    // die Falle wartete also auf den ersten Adapter, der sie nutzt.
    const think: ThinkingEvent = {
      kind: "thinking", uuid: "a1", ts: "2026-08-21T00:00:00Z", text: "ueberlege", sidechain: false,
    };
    const answer = msg("a1", "hier ist die Antwort", "assistant");

    let st = createInitialChatState();
    st = chatReducer(st, think);
    st = chatReducer(st, answer);

    expect(st.events.map((e) => e.kind)).toEqual(["thinking", "message"]);
    expect(st.events[1]).toMatchObject({ text: "hier ist die Antwort" });
  });

  it("verwirft eine ECHTE Dublette weiterhin — gleiche Art, gleiche uuid", () => {
    // Gegenprobe: der Schutz vor doppelt gelieferten Zeilen (Claude Code
    // wiederholt eine Zeile beim Fortsetzen einer Sitzung) darf nicht
    // mitverlorengehen.
    let st = createInitialChatState();
    st = chatReducer(st, msg("b1", "einmal"));
    st = chatReducer(st, msg("b1", "nochmal"));

    expect(st.events).toHaveLength(1);
    expect(st.events[0]).toMatchObject({ text: "einmal" });
  });
});

describe("seedSequence — Historie vor bereits eingetroffenen Live-Zeilen", () => {
  it("stellt die Reihenfolge her, wenn eine Live-Zeile vor der Historie da war", () => {
    // Beide Quellen starten gleichzeitig. Bei einem ARBEITENDEN Agenten kann
    // eine Live-Zeile vor der Historien-Antwort eintreffen — sie stand dann
    // allein in der Liste, und die Historie wurde DAHINTER angehaengt. Zu
    // sehen war die neueste Nachricht ganz oben, das Gespraech darunter, und
    // der Sprung ans Ende landete auf der aeltesten statt der neuesten Zeile.
    const history = [msg("h1", "erste"), msg("h2", "zweite")];
    const live = [msg("l1", "gerade eben")];

    let st = createInitialChatState();
    for (const ev of seedSequence(history, live)) st = chatReducer(st, ev);

    expect(st.events.map((e) => (e as MessageEvent).text)).toEqual([
      "erste", "zweite", "gerade eben",
    ]);
  });

  it("laesst eine Zeile, die in BEIDEN Quellen steht, nur einmal durch", () => {
    // Der Tailer setzt beim Verbinden ans Dateiende auf, die Historie liest
    // bis zum Lesezeitpunkt — eine Zeile kann darum in beiden vorkommen.
    const shared = msg("s1", "doppelt geliefert");

    let st = createInitialChatState();
    for (const ev of seedSequence([shared], [shared])) st = chatReducer(st, ev);

    expect(st.events).toHaveLength(1);
  });
});

describe("reconcilePendingEchoes", () => {
  it("retires the echo whose text the transcript just confirmed", () => {
    const a = echo({ id: "a", text: "erste" });
    const b = echo({ id: "b", text: "zweite" });

    expect(reconcilePendingEchoes([a, b], "zweite")).toEqual([a]);
  });

  it("ignores whitespace differences — the CLI may re-wrap what it received", () => {
    const a = echo({ id: "a", text: "mach  das\n bitte" });

    expect(reconcilePendingEchoes([a], "mach das bitte")).toEqual([]);
  });

  it("retires the oldest echo when nothing matches, rather than risk a double bubble", () => {
    const a = echo({ id: "a", text: "erste" });
    const b = echo({ id: "b", text: "zweite" });

    // A visible duplicate is worse than dropping a local copy of a message that
    // is on screen either way.
    expect(reconcilePendingEchoes([a, b], "etwas ganz anderes")).toEqual([b]);
  });

  it("never doubles: two confirmations retire two echoes", () => {
    const a = echo({ id: "a", text: "erste" });
    const b = echo({ id: "b", text: "zweite" });

    const afterFirst = reconcilePendingEchoes([a, b], "erste");
    expect(reconcilePendingEchoes(afterFirst, "zweite")).toEqual([]);
  });

  it("does nothing when there is no echo to retire", () => {
    expect(reconcilePendingEchoes([], "irgendwas")).toEqual([]);
  });
});

describe("withdrawPendingEcho", () => {
  it("removes the newest echo with that text (the send that just failed)", () => {
    const older = echo({ id: "old", text: "gleich", sentAt: 1_000 });
    const newer = echo({ id: "new", text: "gleich", sentAt: 2_000 });

    expect(withdrawPendingEcho([older, newer], "gleich")).toEqual([older]);
  });

  it("leaves the list alone when the text isn't pending", () => {
    const a = echo({ id: "a", text: "hallo" });
    expect(withdrawPendingEcho([a], "was anderes")).toEqual([a]);
  });
});

describe("markUnconfirmedEchoes", () => {
  it("flips an echo that has gone unacknowledged past the timeout", () => {
    const a = echo({ sentAt: 1_000 });
    const [flipped] = markUnconfirmedEchoes([a], 1_000 + ECHO_CONFIRM_TIMEOUT_MS);

    expect(flipped.status).toBe("unconfirmed");
  });

  // The operator's live bug: a message sent while the agent works is QUEUED by
  // the CLI and only written once the running turn ends. A turn easily outlasts
  // ten seconds, so the timer accused a message that was perfectly safe.

  it("never warns while the agent is mid-turn, no matter how long it takes", () => {
    const a = echo({ sentAt: 1_000 });
    const [result] = markUnconfirmedEchoes([a], 1_000 + ECHO_CONFIRM_TIMEOUT_MS * 10, true);

    expect(result.status).toBe("queued");
  });

  it("shows a mid-turn send as queued straight away", () => {
    const a = echo({ sentAt: 1_000 });
    expect(markUnconfirmedEchoes([a], 1_100, true)[0].status).toBe("queued");
  });

  it("returns a queued echo to waiting once the turn is over", () => {
    // The queue should now drain, so it counts again rather than staying
    // "queued" forever.
    const a = echo({ sentAt: 1_000, status: "queued" });
    expect(markUnconfirmedEchoes([a], 1_100, false)[0].status).toBe("pending");
  });

  it("restarts the clock when the queue drains, instead of accusing at once", () => {
    // Operator-Befund 03.09.2026: die Nachricht sass minutenlang sicher in
    // der Warteschlange; kaum war der Zug vorbei, stand sie SOFORT als
    // "Nicht bestaetigt" da — der Zaehler lief ab dem urspruenglichen
    // Sendezeitpunkt weiter. Die CLI braucht nach dem Zug aber erst ein
    // paar Sekunden, um die Zeile zu schreiben; die zehn Sekunden gehoeren
    // ab dem Ende des Zugs gezaehlt.
    const a = echo({ sentAt: 1_000, status: "queued" });
    const drainedAt = 1_000 + ECHO_CONFIRM_TIMEOUT_MS * 6;
    const [back] = markUnconfirmedEchoes([a], drainedAt, false);
    expect(back.status).toBe("pending");
    expect(back.sentAt).toBe(drainedAt);
    expect(markUnconfirmedEchoes([back], drainedAt + 1_000, false)[0].status).toBe("pending");
  });

  it("still warns on an idle agent after the timeout", () => {
    const a = echo({ sentAt: 1_000 });
    expect(
      markUnconfirmedEchoes([a], 1_000 + ECHO_CONFIRM_TIMEOUT_MS, false)[0].status
    ).toBe("unconfirmed");
  });

  it("never warns about an echo waiting on a starting agent", () => {
    // That one has its own retry in flight.
    const a = echo({ sentAt: 1_000, status: "starting" });
    expect(
      markUnconfirmedEchoes([a], 1_000 + ECHO_CONFIRM_TIMEOUT_MS * 5, false)[0].status
    ).toBe("starting");
  });

  it("leaves a fresh echo pending", () => {
    const a = echo({ sentAt: 1_000 });
    expect(markUnconfirmedEchoes([a], 1_500)[0].status).toBe("pending");
  });

  it("returns the identical array when nothing changed, so no re-render is forced", () => {
    const list = [echo({ sentAt: 1_000 })];
    expect(markUnconfirmedEchoes(list, 1_500)).toBe(list);
  });

  it("does not re-flip an already-unconfirmed echo", () => {
    const list = [echo({ sentAt: 1_000, status: "unconfirmed" })];
    expect(markUnconfirmedEchoes(list, 99_000)).toBe(list);
  });
});


describe("takeQueuedEchoes", () => {
  // Zurueckziehen: die CLI holt mit "Up" ALLE eingereihten Nachrichten auf
  // einmal in die Eingabezeile (queue-operation popAll, live 03.09.2026) —
  // also verschwinden lokal auch alle queued-Echos, und ihr Text kommt in
  // der Reihenfolge des Einreihens zurueck, damit der Verfasser ihn
  // bearbeiten kann.
  it("removes every queued echo and hands their texts back in order", () => {
    const list = [
      echo({ id: "e1", text: "erste", status: "queued" }),
      echo({ id: "e2", text: "bleibt", status: "unconfirmed" }),
      echo({ id: "e3", text: "zweite", status: "queued" }),
    ];
    const { remaining, taken } = takeQueuedEchoes(list);
    expect(taken).toEqual(["erste", "zweite"]);
    expect(remaining.map((e) => e.id)).toEqual(["e2"]);
  });

  it("returns the identical array when nothing is queued", () => {
    const list = [echo({ status: "pending" })];
    expect(takeQueuedEchoes(list).remaining).toBe(list);
  });
});

// ── Aliveness ────────────────────────────────────────────────────────────────
// The rule that stopped the UI from announcing a finished session at one that
// was merely quiet.

describe("resolveAliveness", () => {
  const session = (over: Partial<Parameters<typeof resolveAliveness>[0] & object> = {}) => ({
    sessionId: "s1",
    live: true,
    startedAt: null,
    ...over,
  });

  it("trusts the server's answer when it has one", () => {
    expect(resolveAliveness(session({ aliveness: "ended", live: true }))).toBe("ended");
    expect(resolveAliveness(session({ aliveness: "idle", live: true }))).toBe("idle");
    expect(resolveAliveness(session({ aliveness: "active", live: false }))).toBe("active");
  });

  it("reads a live session as active when the server field is missing", () => {
    expect(resolveAliveness(session({ live: true }))).toBe("active");
  });

  it("reads a NON-live session as idle, never as ended", () => {
    // This is the whole point: `live` is mtime-based, and a stale mtime is not
    // evidence that a session finished. Only the server may claim `ended`.
    expect(resolveAliveness(session({ live: false }))).toBe("idle");
  });

  it("treats a missing session as idle rather than ended", () => {
    expect(resolveAliveness(null)).toBe("idle");
    expect(resolveAliveness(undefined)).toBe("idle");
  });
});


describe("reconcilePendingEchoes — history vs live", () => {
  it("retires an echo the refetched history confirms by text", () => {
    // After a session rollover the confirmation can arrive ONLY through the
    // refetched history; without this the echo would dangle into a false warning.
    const a = echo({ id: "a", text: "mach weiter" });
    expect(
      reconcilePendingEchoes([a], "mach weiter", { allowOldestFallback: false })
    ).toEqual([]);
  });

  it("does NOT retire an echo against an unrelated history message", () => {
    // A rollover page is full of other user messages; the oldest-echo fallback
    // would claim a delivery that never happened.
    const a = echo({ id: "a", text: "mach weiter" });
    expect(
      reconcilePendingEchoes([a], "etwas voellig anderes", { allowOldestFallback: false })
    ).toEqual([a]);
  });

  it("keeps the fallback for a live event, where it is the safer choice", () => {
    const a = echo({ id: "a", text: "mach weiter" });
    expect(reconcilePendingEchoes([a], "umformatiert")).toEqual([]);
  });
});

describe("markEchoStarting / markEchoRetried", () => {
  it("marks the newest matching echo as waiting on a starting agent", () => {
    const older = echo({ id: "old", text: "gleich", sentAt: 1_000 });
    const newer = echo({ id: "new", text: "gleich", sentAt: 2_000 });

    const [a, b] = markEchoStarting([older, newer], "gleich");
    expect(a.status).toBe("pending");
    expect(b.status).toBe("starting");
  });

  it("records the spent retry and returns the echo to ordinary waiting", () => {
    const a = echo({ text: "gleich", status: "starting" });
    const [result] = markEchoRetried([a], "gleich");

    expect(result.status).toBe("pending");
    expect(result.retried).toBe(true);
  });

  it("leaves the list alone when no echo matches", () => {
    const a = echo({ text: "hallo" });
    expect(markEchoStarting([a], "anderes")).toEqual([a]);
    expect(markEchoRetried([a], "anderes")).toEqual([a]);
  });
});

describe("chatReducer — session rollover", () => {
  it("clears the transcript but cannot touch pending echoes", () => {
    // Echoes live OUTSIDE the reducer on purpose (it is a pure projection of the
    // transcript). That design choice is also what makes them survive a
    // rollover — a send that triggers one must not lose its bubble.
    const seeded = chatReducer(createInitialChatState(), {
      kind: "message",
      uuid: "m1",
      ts: "2026-08-17T10:00:00Z",
      role: "assistant",
      text: "hi",
      model: null,
      sidechain: false,
    });
    const rolled = chatReducer(seeded, { kind: "session_changed" });

    expect(rolled.events).toEqual([]);
    expect(Object.keys(rolled)).not.toContain("pendingEchoes");
  });
});


describe("chatReducer — state freshness", () => {
  // "Status settle": the frontend must never keep an older probe result over a
  // newer one. It doesn't cache at all — every state frame replaces the last —
  // so the only residual staleness is the probe's own ~2s poll interval, which
  // is the backend's to shorten, not something the UI can honestly paper over.
  it("always takes the newest state frame", () => {
    let st = chatReducer(createInitialChatState(), {
      kind: "state",
      status: "working",
      prompt: null,
    });
    expect(st.state?.status).toBe("working");

    st = chatReducer(st, { kind: "state", status: "idle", prompt: null });
    expect(st.state?.status).toBe("idle");

    // And back again — no stickiness in either direction.
    st = chatReducer(st, { kind: "state", status: "working", prompt: null });
    expect(st.state?.status).toBe("working");
  });

  it("does not let an assistant message resurrect or freeze a state", () => {
    let st = chatReducer(createInitialChatState(), {
      kind: "state",
      status: "idle",
      prompt: null,
    });
    st = chatReducer(st, {
      kind: "message",
      uuid: "m1",
      ts: "2026-08-17T10:00:00Z",
      role: "assistant",
      text: "fertig",
      model: null,
      sidechain: false,
    });

    // Messages carry no status claim; the probe stays the only source.
    expect(st.state?.status).toBe("idle");
  });
});


// ── /clear: der Rollover IST die Bestaetigung (Operator-Befund 19.08.2026) ──

describe("echoes answered by a session rollover", () => {
  const echo = (text: string) => ({ id: "e1", text, sentAt: 0, status: "pending" as const });

  it("retires a /clear echo — its confirmation can never appear in the new file", () => {
    const out = retireEchoesAnsweredByRollover([echo("/clear")]);
    expect(out).toEqual([]);
  });

  it("keeps a normal message — a rollover for another reason must not fake delivery", () => {
    // Recycler-Respawn oder Auto-Compact der CLI rollen die Datei ebenfalls;
    // dort waere ein Abhaken eine Luege (Rollover-Haerte aus R14a).
    const echoes = [echo("mach bitte X")];
    expect(retireEchoesAnsweredByRollover(echoes)).toBe(echoes);
  });

  it("recognises the command regardless of case and trailing args", () => {
    expect(isSessionClearingCommand("  /CLEAR  ")).toBe(true);
    expect(isSessionClearingCommand("/clear now")).toBe(true);
    // /compact verdichtet INNERHALB der Session — kein Rollover-Beweis.
    expect(isSessionClearingCommand("/compact")).toBe(false);
    expect(isSessionClearingCommand("bitte /clear machen")).toBe(false);
  });
});
