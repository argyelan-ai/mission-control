/**
 * Critic rework (S3 item D): the speed line is a door only where a switch
 * can really happen; the pair it names comes from the record chat, not
 * from the switch rows; a model-only switch never masks the header state
 * and never keeps the fast poll running.
 */
import { describe, expect, it } from "vitest";
import { canPickPair, isSwitching, switchPollInterval, SWITCH_POLL_PENDING_MS, SWITCH_POLL_IDLE_MS, SWITCH_FAST_POLL_FOR_MS } from "@/hooks/useChatSwitch";
import { pairForLine } from "./SpeedLine";
import type { RecordChat, SwitchRow } from "@/lib/api";

const chat = (over: Partial<RecordChat> = {}): RecordChat => ({
  id: "chat-1", title: "Demo", harness: "omp", model: "qwen38", legacyAgentId: "agent-1", agentDefault: true,
  nativeSessionId: null, pinned: false, archived: false, lastActivityAt: null, createdAt: null, runtimeId: "rt-REC",
  ...over,
});
const row = (over: Partial<SwitchRow>): SwitchRow => ({ eventId: "e", ts: "2026-11-19T10:00:00Z", status: "pending", path: "restart", ...over });
const NOW = Date.parse("2026-11-19T10:00:30Z");

describe("canPickPair — the line is a door only where a switch can happen", () => {
  it("main chat (follows the agent): never a door", () => {
    expect(canPickPair(chat({ agentDefault: true }), true)).toBe(false);
  });
  it("viewer: never a door (every switch needs ADMIN)", () => {
    expect(canPickPair(chat({ agentDefault: false }), false)).toBe(false);
  });
  it("record off: no door", () => {
    expect(canPickPair(null, true)).toBe(false);
  });
  it("further chat + admin: door", () => {
    expect(canPickPair(chat({ agentDefault: false }), true)).toBe(true);
  });
});

describe("pairForLine — the record chat is the truth, never the switch rows", () => {
  const fallback = { runtimeId: "rt-AGENT", model: "agent-model" };
  it("main chat: the agent binding (an agent runtime switch writes no switch row)", () => {
    expect(pairForLine(chat({ agentDefault: true, runtimeId: "rt-OLD", model: "sonnet" }), fallback)).toEqual(fallback);
  });
  it("further chat: the record chat's own pair", () => {
    expect(pairForLine(chat({ agentDefault: false, runtimeId: "rt-B", model: "qwen" }), fallback)).toEqual({ runtimeId: "rt-B", model: "qwen" });
  });
  it("further chat without a model yet: the box of the chat, model of the binding only if same box", () => {
    expect(pairForLine(chat({ agentDefault: false, runtimeId: "rt-B", model: null }), fallback)).toEqual({ runtimeId: "rt-B", model: null });
  });
  it("record off: the binding", () => {
    expect(pairForLine(null, fallback)).toEqual(fallback);
  });
});

describe("isSwitching — only a restart/switch-back masks the header state", () => {
  it("a pending model-only switch is not a header state", () => {
    expect(isSwitching([row({ path: "model_only" })])).toBe(false);
  });
  it("a pending restart is", () => {
    expect(isSwitching([row({ path: "restart" })])).toBe(true);
    expect(isSwitching([row({ path: "switch_back" })])).toBe(true);
  });
  it("finished rows are not", () => {
    expect(isSwitching([row({ status: "done" }), row({ status: "failed" })])).toBe(false);
  });
});

describe("switchPollInterval — the fast poll is short-lived", () => {
  it("nothing open: no polling at all", () => {
    expect(switchPollInterval([row({ status: "done" })], NOW)).toBe(false);
  });
  it("a young restart: fast", () => {
    expect(switchPollInterval([row({ path: "restart" })], NOW)).toBe(SWITCH_POLL_PENDING_MS);
  });
  it("a restart older than the fast window: calm", () => {
    expect(switchPollInterval([row({ path: "restart" })], NOW + SWITCH_FAST_POLL_FOR_MS)).toBe(SWITCH_POLL_IDLE_MS);
  });
  it("a model-only switch (waits for the next answer, no cutoff): calm, never fast", () => {
    expect(switchPollInterval([row({ path: "model_only" })], NOW)).toBe(SWITCH_POLL_IDLE_MS);
  });
});
