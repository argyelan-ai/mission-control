import { describe, it, expect } from "vitest";
import type { ToolEvent } from "@/lib/chatTypes";
import {
  isReachable,
  pairTitle,
  parseSendCall,
  reachableCount,
  sendOutcome,
  type DirectoryRow,
} from "../comms";

function tool(name: string, detail: Record<string, unknown>, result: string | null = null, status: "done" | "error" = "done"): ToolEvent {
  return {
    kind: "tool", uuid: "u1", ts: "2026-11-02T10:00:00Z", name, title: name, detail,
    toolUseId: "t1", result, status, stats: null, sidechain: false,
  };
}

function row(o: Partial<DirectoryRow>): DirectoryRow {
  return {
    address: "lead", kind: "agent", label: "Lead", harness: "omp", machine: null,
    alive: "yes", listening: true, busy: false, accept: "accept", task: null, last_seen: null, ...o,
  };
}

describe("parseSendCall", () => {
  it("reads the MCP tool under any server prefix", () => {
    expect(parseSendCall(tool("mcp__mc__mc_send", { to: "worker", body: "Is box one free?" }))).toEqual({
      to: "worker", body: "Is box one free?",
    });
    expect(parseSendCall(tool("mc_send", { to: "worker", body: "x" }))?.to).toBe("worker");
  });

  it("reads `mc send <to> <text>` with quotes, in either shell tool spelling", () => {
    expect(parseSendCall(tool("Bash", { command: 'mc send claude@desk:runtimes "When is the test done?"' }))).toEqual({
      to: "claude@desk:runtimes", body: "When is the test done?",
    });
    expect(parseSendCall(tool("bash", { command: "mc send worker 'it''s fine'" }))?.body).toBe("its fine");
    expect(parseSendCall(tool("Bash", { command: "/usr/local/bin/mc send worker hi" }))?.to).toBe("worker");
  });

  it("text from stdin has no body to show", () => {
    expect(parseSendCall(tool("Bash", { command: "mc send worker -" }))).toEqual({ to: "worker", body: null });
  });

  it("leaves everything else a tool row", () => {
    expect(parseSendCall(tool("Bash", { command: "mc who" }))).toBeNull();
    expect(parseSendCall(tool("Bash", { command: "mc send worker hi && rm -rf x" }))).toBeNull();
    expect(parseSendCall(tool("Bash", { command: "echo hi | mc send worker -" }))).toBeNull();
    expect(parseSendCall(tool("Bash", { command: "mc send worker" }))).toBeNull();
    expect(parseSendCall(tool("Read", { command: "mc send worker hi" }))).toBeNull();
    expect(parseSendCall(tool("mcp__mc__mc_msg", { to: "worker", body: "x" }))).toBeNull();
    expect(parseSendCall(tool("mcp__mc__mc_send", { body: "x" }))).toBeNull();
  });

  it("an `&&` inside the quoted text is just text", () => {
    expect(parseSendCall(tool("Bash", { command: 'mc send worker "build && test"' }))?.body).toBe("build && test");
  });
});

describe("sendOutcome", () => {
  it("pending until a result lands", () => {
    expect(sendOutcome(tool("Bash", {}, null))).toEqual({ kind: "pending" });
  });
  it("reads delivery and the brake from the CLI and the MCP wording", () => {
    expect(sendOutcome(tool("Bash", {}, "sent to worker · queued_busy"))).toEqual({
      kind: "sent", delivery: "queued_busy", paused: false,
    });
    expect(
      sendOutcome(tool("mc_send", {}, "gesendet an worker · next_rest · Gespraech pausiert, wartet auf die Freigabe")),
    ).toEqual({ kind: "sent", delivery: "next_rest", paused: true });
  });
  it("a refusal is a failure with its reason", () => {
    expect(sendOutcome(tool("Bash", {}, "worker is paused or not listening.", "error"))).toEqual({
      kind: "failed", reason: "worker is paused or not listening.",
    });
    expect(sendOutcome(tool("mc_send", {}, "Fehler: 409 not_listening"))).toEqual({
      kind: "failed", reason: "409 not_listening",
    });
  });
});

describe("reachable", () => {
  it("needs alive AND listening — a paused agent is alive but not reachable", () => {
    expect(isReachable(row({}))).toBe(true);
    expect(isReachable(row({ listening: false }))).toBe(false);
    expect(isReachable(row({ alive: "unknown" }))).toBe(false);
    expect(isReachable(row({ listening: null }))).toBe(false);
    expect(reachableCount({ sessions: [row({}), row({ listening: false }), row({ alive: "no" })], warming_up: false })).toBe(1);
    expect(reachableCount(undefined)).toBe(0);
  });

  it("pairTitle keeps the backend order", () => {
    expect(
      pairTitle({
        participants: [
          { ref: "a", kind: "agent", address: "lead", label: "Lead", harness: null },
          { ref: "b", kind: "session", address: "claude@desk:x", label: "x", harness: "claude" },
        ],
      }),
    ).toBe("lead ↔ claude@desk:x");
  });
});
