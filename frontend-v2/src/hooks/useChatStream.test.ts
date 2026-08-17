import { describe, it, expect } from "vitest";
import {
  chatReducer,
  createInitialChatState,
  markUnconfirmedEchoes,
  reconcilePendingEchoes,
  withdrawPendingEcho,
  ECHO_CONFIRM_TIMEOUT_MS,
  MAX_CHAT_EVENTS,
  type PendingEcho,
} from "./useChatStream";
import type {
  ChatEvent,
  CommandEvent,
  MessageEvent,
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

  it("preserves state/usage slots across a session_changed reset", () => {
    let state = chatReducer(createInitialChatState(), {
      kind: "state",
      status: "idle",
      prompt: null,
    } as StateEvent);
    state = chatReducer(state, msg("u1"));
    state = chatReducer(state, { kind: "session_changed" } as SessionChangedEvent);
    expect(state.state).toEqual({ kind: "state", status: "idle", prompt: null });
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
