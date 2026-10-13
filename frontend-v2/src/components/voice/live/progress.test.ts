/**
 * Voice PR 3 — "listen along": which chat-stream changes become a progress
 * item for the voice, when the voice may speak about one, and the guard that
 * catches a success claim before the agent reported one.
 */
import { describe, expect, it } from "vitest";
import type { MessageEvent, StateEvent, TimelineChatEvent, TodoEvent, ToolEvent } from "@/lib/chatTypes";
import {
  FINAL_SETTLE_MS,
  FINAL_WAIT_MS,
  HEARTBEAT_MS,
  PERIODIC_MS,
  ProgressTracker,
  QUIET_MS,
  SOFT_GAP_MS,
  TOOL_MIN_GAP_MS,
  claimsSuccess,
  contextItemEvent,
  speakDecision,
  type ProgressSnapshot,
} from "./progress";

const state = (status: StateEvent["status"], question?: string): StateEvent => ({
  kind: "state",
  status,
  prompt: question ? { question, options: [] } : null,
});

let seq = 0;
const tool = (over: Partial<ToolEvent> = {}): ToolEvent => ({
  kind: "tool",
  uuid: `t${++seq}`,
  ts: "2026-10-13T10:00:00Z",
  name: "Bash",
  title: "for i in $(seq 30); do sleep 1; done",
  detail: {},
  toolUseId: `use-${seq}`,
  result: null,
  status: "done",
  stats: null,
  sidechain: false,
  ...over,
});
const msg = (text: string, over: Partial<MessageEvent> = {}): MessageEvent => ({
  kind: "message",
  uuid: `m${++seq}`,
  ts: "2026-10-13T10:00:00Z",
  role: "assistant",
  text,
  model: null,
  sidechain: false,
  ...over,
});
const todos = (...items: [string, "pending" | "in_progress" | "completed"][]): TodoEvent => ({
  kind: "todo",
  uuid: `td${++seq}`,
  ts: "2026-10-13T10:00:00Z",
  source: "acp_plan",
  items: items.map(([content, status]) => ({ content, status })),
});
const snap = (s: StateEvent | null, events: TimelineChatEvent[] = [], td: TodoEvent | null = null): ProgressSnapshot => ({
  state: s,
  events,
  todos: td,
});
const kinds = (ms: { req: { kind: string } }[]) => ms.map((m) => m.req.kind);

describe("ProgressTracker — baseline", () => {
  it("the first look is a baseline: history never becomes milestones", () => {
    const t = new ProgressTracker();
    const out = t.observe(snap(state("idle"), [tool({ result: "ok" }), msg("old answer")]), 0);
    expect(out).toEqual([]);
  });

  it("a busy agent at call start gives one silent snapshot with step and running tool", () => {
    const t = new ProgressTracker();
    const out = t.observe(
      snap(state("working"), [tool({ title: "npm test" })], todos(["Plan", "completed"], ["Tests", "in_progress"])),
      0,
    );
    expect(kinds(out)).toEqual(["snapshot"]);
    expect(out[0].speak).toBe("never");
    expect(out[0].req.step).toEqual({ index: 2, total: 2, content: "Tests" });
    expect(out[0].req.tool).toEqual({ name: "Bash", title: "npm test" });
  });
});

describe("ProgressTracker — milestones", () => {
  it("forwarded request → agent starts → 'started' (spoken)", () => {
    const t = new ProgressTracker();
    t.observe(snap(state("idle")), 0);
    t.forwarded(false, 1000);
    const out = t.observe(snap(state("working")), 2000);
    expect(kinds(out)).toEqual(["started"]);
    expect(out[0].speak).toBe("always");
  });

  it("the agent starting on its own is context only ('working')", () => {
    const t = new ProgressTracker();
    t.observe(snap(state("idle")), 0);
    const out = t.observe(snap(state("working")), 1000);
    expect(kinds(out)).toEqual(["working"]);
    expect(out[0].speak).toBe("never");
  });

  it("tool start (soft) and finish (silent) carry the title, never the output", () => {
    const t = new ProgressTracker();
    t.observe(snap(state("working")), 0);
    const running = tool();
    let out = t.observe(snap(state("working"), [running]), 1000);
    expect(kinds(out)).toEqual(["tool_started"]);
    expect(out[0].speak).toBe("soft");
    out = t.observe(
      snap(state("working"), [{ ...running, result: "SECRET OUTPUT", status: "error" }]),
      1000 + TOOL_MIN_GAP_MS,
    );
    expect(kinds(out)).toEqual(["tool_finished"]);
    expect(out[0].speak).toBe("never");
    expect(out[0].req.tool).toEqual({ name: "Bash", title: running.title, failed: true });
    expect(JSON.stringify(out)).not.toContain("SECRET OUTPUT");
  });

  it("subagent (sidechain) tools are ignored", () => {
    const t = new ProgressTracker();
    t.observe(snap(state("working")), 0);
    expect(t.observe(snap(state("working"), [tool({ sidechain: true })]), 1000)).toEqual([]);
  });

  it("a burst of tools is throttled: the newest is held and flushed by tick", () => {
    const t = new ProgressTracker();
    t.observe(snap(state("working")), 0);
    const a = tool({ title: "a" });
    const b = tool({ title: "b" });
    const c = tool({ title: "c" });
    expect(kinds(t.observe(snap(state("working"), [a]), 1000))).toEqual(["tool_started"]);
    expect(t.observe(snap(state("working"), [a, b]), 1500)).toEqual([]);
    expect(t.observe(snap(state("working"), [a, b, c]), 2000)).toEqual([]);
    expect(t.tick(2500)).toEqual([]);
    const flushed = t.tick(1000 + TOOL_MIN_GAP_MS);
    expect(kinds(flushed)).toEqual(["tool_started"]);
    expect(flushed[0].req.tool?.title).toBe("c");
  });

  it("todo step change → 'step' (soft); all checked off → step with all_done", () => {
    const t = new ProgressTracker();
    t.observe(snap(state("working"), [], todos(["A", "in_progress"], ["B", "pending"])), 0);
    let out = t.observe(snap(state("working"), [], todos(["A", "completed"], ["B", "in_progress"])), 1000);
    expect(kinds(out)).toEqual(["step"]);
    expect(out[0].req.step).toEqual({ index: 2, total: 2, content: "B" });
    expect(out[0].speak).toBe("soft");
    // Same step again (a new snapshot object): nothing.
    expect(t.observe(snap(state("working"), [], todos(["A", "completed"], ["B", "in_progress"])), 2000)).toEqual([]);
    out = t.observe(snap(state("working"), [], todos(["A", "completed"], ["B", "completed"])), 3000);
    expect(out[0].req.step).toEqual({ index: 2, total: 2, content: "", all_done: true });
  });

  it("approval and question are spoken at once, with the question", () => {
    const t = new ProgressTracker();
    t.observe(snap(state("working")), 0);
    let out = t.observe(snap(state("permission_prompt", "Allow rm?")), 1000);
    expect(kinds(out)).toEqual(["approval"]);
    expect(out[0].req.question).toBe("Allow rm?");
    expect(out[0].speak).toBe("always");
    out = t.observe(snap(state("waiting_input", "Which branch?")), 2000);
    expect(kinds(out)).toEqual(["question"]);
  });

  it("a chat-daemon error message → 'error' with its code only", () => {
    const t = new ProgressTracker();
    t.observe(snap(state("working")), 0);
    const err = msg("Provider failed: long detail", {
      role: "teammate",
      source: { kind: "error", title: null },
      error: { code: "provider_error", detail: "long detail" },
    });
    const out = t.observe(snap(state("working"), [err]), 1000);
    expect(kinds(out)).toEqual(["error"]);
    expect(out[0].req).toEqual({ kind: "error", error_code: "provider_error" });
  });
});

describe("ProgressTracker — final (never success before the agent said so)", () => {
  it("final only after the turn settled, with the agent's own last reply", () => {
    const t = new ProgressTracker();
    t.observe(snap(state("idle")), 0);
    t.forwarded(false, 0);
    t.observe(snap(state("working")), 1000);
    expect(t.guardArmed()).toBe(true);
    const reply = msg("Die Schleife ist durch. Alles grün.");
    expect(kinds(t.observe(snap(state("idle"), [reply]), 40_000))).toEqual([]);
    // A flicker back to working cancels the pending final.
    t.observe(snap(state("working"), [reply]), 40_500);
    expect(t.tick(40_500 + FINAL_SETTLE_MS)).toEqual([]);
    t.observe(snap(state("idle"), [reply]), 41_000);
    expect(t.tick(41_000 + FINAL_SETTLE_MS - 1)).toEqual([]);
    const out = t.tick(41_000 + FINAL_SETTLE_MS);
    expect(kinds(out)).toEqual(["final"]);
    expect(out[0].req.text).toBe("Die Schleife ist durch. Alles grün.");
    expect(out[0].speak).toBe("always");
    expect(t.guardArmed()).toBe(false);
    expect(t.tick(60_000)).toEqual([]); // once only
  });

  it("omp writes the reply after going idle: final waits for it", () => {
    const t = new ProgressTracker();
    t.observe(snap(state("idle")), 0);
    t.forwarded(false, 0);
    t.observe(snap(state("working")), 1000);
    t.observe(snap(state("idle")), 30_000);
    expect(t.tick(30_000 + FINAL_SETTLE_MS)).toEqual([]);
    t.observe(snap(state("idle"), [msg("OK")]), 33_000);
    const out = t.tick(33_100);
    expect(out[0].req).toEqual({ kind: "final", text: "OK" });
  });

  it("no reply at all: final without text after the wait", () => {
    const t = new ProgressTracker();
    t.observe(snap(state("idle")), 0);
    t.observe(snap(state("working")), 1000);
    t.observe(snap(state("idle")), 2000);
    expect(t.tick(2000 + FINAL_WAIT_MS - 1)).toEqual([]);
    expect(t.tick(2000 + FINAL_WAIT_MS)[0].req).toEqual({ kind: "final", text: "" });
  });

  it("a fast turn never seen as 'working' still ends in a final", () => {
    const t = new ProgressTracker();
    t.observe(snap(state("idle")), 0);
    t.forwarded(false, 0);
    t.observe(snap(state("idle"), [msg("Done: OK")]), 3000);
    expect(kinds(t.tick(3000 + FINAL_SETTLE_MS))).toEqual(["final"]);
  });

  it("an idle agent without any request gives no final", () => {
    const t = new ProgressTracker();
    t.observe(snap(state("idle")), 0);
    t.observe(snap(state("idle"), [msg("random")]), 1000);
    expect(t.tick(1000 + FINAL_WAIT_MS)).toEqual([]);
    expect(t.guardArmed()).toBe(false);
  });

  it("the reply is capped before it leaves the browser", () => {
    const t = new ProgressTracker();
    t.observe(snap(state("idle")), 0);
    t.forwarded(false, 0);
    t.observe(snap(state("idle"), [msg("x".repeat(20_000))]), 1000);
    expect((t.tick(1000 + FINAL_SETTLE_MS)[0].req.text ?? "").length).toBeLessThanOrEqual(4000);
  });
});

describe("ProgressTracker — periodic 'still working' (throttle)", () => {
  it("nothing changed → nothing sent until the heartbeat", () => {
    const t = new ProgressTracker();
    t.observe(snap(state("idle")), 0);
    t.observe(snap(state("working"), [tool()]), 1000); // working + tool_started (sent)
    expect(t.tick(1000 + PERIODIC_MS)).toEqual([]);
    expect(t.tick(1000 + HEARTBEAT_MS - 1)).toEqual([]);
    const out = t.tick(1000 + HEARTBEAT_MS);
    expect(kinds(out)).toEqual(["still_working"]);
    expect(out[0].speak).toBe("periodic");
    expect(out[0].req.elapsed_s).toBe(60);
    expect(out[0].req.tool?.name).toBe("Bash");
    // at most one per window
    expect(t.tick(1000 + HEARTBEAT_MS + PERIODIC_MS)).toEqual([]);
  });

  it("no periodic line while idle", () => {
    const t = new ProgressTracker();
    t.observe(snap(state("idle")), 0);
    expect(t.tick(HEARTBEAT_MS * 3)).toEqual([]);
  });
});

describe("speakDecision — never interrupt the operator", () => {
  const floor = { userSpeaking: false, voiceBusy: false, lastUserSpeechAt: -1e9, lastSpokenProgressAt: -1e9 };
  it("milestones speak now, or later when someone is talking", () => {
    expect(speakDecision("always", floor, 0)).toBe("now");
    expect(speakDecision("always", { ...floor, userSpeaking: true }, 0)).toBe("later");
    expect(speakDecision("always", { ...floor, voiceBusy: true }, 0)).toBe("later");
  });
  it("soft milestones: only when the floor is free and not right after another", () => {
    expect(speakDecision("soft", floor, 100_000)).toBe("now");
    expect(speakDecision("soft", { ...floor, lastSpokenProgressAt: 100_000 - SOFT_GAP_MS + 1 }, 100_000)).toBe("no");
    expect(speakDecision("soft", { ...floor, userSpeaking: true }, 100_000)).toBe("no");
  });
  it("periodic: only after operator silence and at most once per heartbeat", () => {
    expect(speakDecision("periodic", floor, 100_000)).toBe("now");
    expect(speakDecision("periodic", { ...floor, lastUserSpeechAt: 100_000 - QUIET_MS + 1 }, 100_000)).toBe("no");
    expect(speakDecision("periodic", { ...floor, lastSpokenProgressAt: 100_000 - HEARTBEAT_MS + 1 }, 100_000)).toBe("no");
    expect(speakDecision("periodic", { ...floor, voiceBusy: true }, 100_000)).toBe("no");
  });
  it("context-only items never speak", () => {
    expect(speakDecision("never", floor, 0)).toBe("no");
  });
});

describe("claimsSuccess — the no-success guard", () => {
  it.each([
    "Erledigt, ich hab's weitergegeben.",
    "Er ist fertig.",
    "Done!",
    "The tests are finished and everything passed.",
    "Geschafft, alles grün.",
    "Das ist abgeschlossen.",
  ])("flags %s", (s) => expect(claimsSuccess(s)).toBe(true));

  it.each([
    "Noch nicht fertig, er arbeitet noch.",
    "Not done yet, he is still working.",
    "Sobald er fertig ist, sag ich Bescheid.",
    "I'll tell you when he's done.",
    "Er ist nicht fertig.",
    "Passed on, he is working on it.",
    "It isn't finished.",
    "Korrektur: noch nicht erledigt.",
  ])("does not flag %s", (s) => expect(claimsSuccess(s)).toBe(false));
});

describe("contextItemEvent", () => {
  it("is a system message item (no response by itself)", () => {
    expect(contextItemEvent("[MC progress] x")).toEqual({
      type: "conversation.item.create",
      item: { type: "message", role: "system", content: [{ type: "input_text", text: "[MC progress] x" }] },
    });
  });
});
