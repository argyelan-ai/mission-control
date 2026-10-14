/** Reducer side of chat package P3: question cards stay in the timeline,
 *  their outcome and the session mode live in side slots. */
import { describe, expect, it } from "vitest";

import type { ElicitationEvent, ElicitationResultEvent, ModeEvent } from "@/lib/chatTypes";
import { chatReducer, createInitialChatState } from "./useChatStream";

const question: ElicitationEvent = {
  kind: "elicitation", uuid: "acp00010", ts: "2026-10-13T10:00:00Z",
  elicitationId: "el-0123456789ab", message: "Which?",
  schema: { type: "object", properties: { value: { type: "boolean" } } },
};

const outcome = (status: ElicitationResultEvent["status"], uuid: string): ElicitationResultEvent => ({
  kind: "elicitation_result", uuid, ts: "2026-10-13T10:01:00Z",
  elicitationId: "el-0123456789ab", status, content: status === "answered" ? { value: true } : null,
});

const mode = (modeId: string, ts: string, uuid: string): ModeEvent => ({ kind: "mode", uuid, ts, modeId });

describe("chatReducer — P3", () => {
  it("a question is a timeline row, its outcome is not", () => {
    let s = chatReducer(createInitialChatState(), question);
    s = chatReducer(s, outcome("answered", "acp00011"));
    expect(s.events.map((e) => e.kind)).toEqual(["elicitation"]);
    expect(s.elicitationResults["el-0123456789ab"].status).toBe("answered");
  });

  it("the first outcome wins", () => {
    let s = chatReducer(createInitialChatState(), outcome("answered", "acp00011"));
    s = chatReducer(s, outcome("expired", "acp00012"));
    expect(s.elicitationResults["el-0123456789ab"].status).toBe("answered");
  });

  it("newest mode wins, an older history line never overrides it", () => {
    let s = chatReducer(createInitialChatState(), mode("plan", "2026-10-13T10:05:00Z", "m2"));
    s = chatReducer(s, mode("default", "2026-10-13T10:00:00Z", "m1"));
    expect(s.mode?.modeId).toBe("plan");
  });

  it("a session rollover forgets outcomes and mode", () => {
    let s = chatReducer(createInitialChatState(), question);
    s = chatReducer(s, outcome("answered", "acp00011"));
    s = chatReducer(s, mode("plan", "2026-10-13T10:05:00Z", "m2"));
    s = chatReducer(s, { kind: "session_changed" });
    expect(s.events).toEqual([]);
    expect(s.elicitationResults).toEqual({});
    expect(s.mode).toBeNull();
  });
});
