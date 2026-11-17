/**
 * filterByDetailLevel — which chat events a detail level shows. The new
 * level "answer" keeps the operator's messages and the agent's LAST reply of
 * each turn; questions (elicitations) and errors stay in every level.
 */
import { describe, it, expect } from "vitest";
import { filterByDetailLevel } from "./detailLevelFilter";
import type { ElicitationEvent, MessageEvent, TimelineChatEvent, ToolEvent } from "@/lib/chatTypes";

let n = 0;
function msg(role: MessageEvent["role"], text: string, over: Partial<MessageEvent> = {}): MessageEvent {
  n += 1;
  return { kind: "message", uuid: `m${n}`, ts: `2026-08-17T10:00:${String(n).padStart(2, "0")}Z`, role, text, model: null, sidechain: false, ...over };
}
function tool(title: string): ToolEvent {
  n += 1;
  return {
    kind: "tool", uuid: `t${n}`, ts: "2026-08-17T10:00:00Z", name: "Read", title, detail: {},
    toolUseId: `tu${n}`, result: "ok", status: "done", stats: null, sidechain: false,
  };
}
function question(): ElicitationEvent {
  n += 1;
  return { kind: "elicitation", uuid: `e${n}`, ts: "2026-08-17T10:00:00Z", elicitationId: `el${n}`, message: "Which one?" } as ElicitationEvent;
}
const texts = (evs: TimelineChatEvent[]) => evs.map((e) => (e.kind === "message" ? e.text : e.kind));

describe("filterByDetailLevel — answer only", () => {
  it("keeps the operator's message and only the agent's last reply of each turn", () => {
    const events = [
      msg("user", "Q1"), msg("assistant", "Let me look"), tool("Read a"), msg("assistant", "A1"),
      msg("user", "Q2"), tool("Read b"), msg("assistant", "A2a"), msg("assistant", "A2"),
    ];
    expect(texts(filterByDetailLevel(events, "answer"))).toEqual(["Q1", "A1", "Q2", "A2"]);
  });

  it("keeps questions and errors, wherever they stand", () => {
    const err = msg("teammate", "boom", { source: { kind: "error", title: null }, error: { code: "rpc_error" } });
    const events = [msg("user", "Q"), question(), msg("assistant", "mid"), err, msg("assistant", "A")];
    expect(texts(filterByDetailLevel(events, "answer"))).toEqual(["Q", "elicitation", "boom", "A"]);
  });

  it("hides a subagent's sidechain reply — it is not the agent's answer", () => {
    const events = [msg("user", "Q"), msg("assistant", "A"), msg("assistant", "side", { sidechain: true })];
    expect(texts(filterByDetailLevel(events, "answer"))).toEqual(["Q", "A"]);
  });
});

describe("filterByDetailLevel — the other levels", () => {
  it("compact keeps a question card too (it used to vanish there)", () => {
    expect(texts(filterByDetailLevel([msg("user", "Q"), question(), tool("Read")], "compact"))).toEqual(["Q", "elicitation"]);
  });

  it("normal and verbose keep everything", () => {
    const events = [msg("user", "Q"), tool("Read"), msg("assistant", "A")];
    expect(filterByDetailLevel(events, "normal")).toEqual(events);
    expect(filterByDetailLevel(events, "verbose")).toEqual(events);
  });
});
