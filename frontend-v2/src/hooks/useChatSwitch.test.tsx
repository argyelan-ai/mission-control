/**
 * The chat switch in the timeline (ADR-105, S3 item D): switch rows come
 * from MC's record and are laid into the transcript at their time — and
 * while the record is off (404) nothing changes at all.
 */
import { describe, expect, it, vi } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

const apiMock = vi.hoisted(() => ({
  listChats: vi.fn(),
  history: vi.fn(),
}));
vi.mock("@/lib/api", () => ({
  api: { chatRecord: { listChats: apiMock.listChats, history: apiMock.history } },
}));

import { defaultRecordChat, mergeSwitchEvents, switchRowsOf, useChatSwitch } from "./useChatSwitch";
import type { RecordChat, SwitchRow } from "@/lib/api";
import type { TimelineChatEvent } from "@/lib/chatTypes";

function msg(uuid: string, ts: string): TimelineChatEvent {
  return { kind: "message", uuid, ts, role: "user", text: uuid, model: null, sidechain: false };
}

function row(eventId: string, ts: string, status: SwitchRow["status"] = "done"): SwitchRow {
  return {
    eventId,
    ts,
    status,
    path: "restart",
    from: { harness: "claude", runtimeId: "rt-a", model: "opus" },
    to: { harness: "omp", runtimeId: "rt-b", model: "qwen" },
  };
}

const chat = (over: Partial<RecordChat> = {}): RecordChat => ({
  id: "chat-1",
  title: "Demo",
  harness: "claude",
  model: "opus",
  legacyAgentId: "agent-1",
  agentDefault: true,
  nativeSessionId: null,
  pinned: false,
  archived: false,
  lastActivityAt: null,
  createdAt: null,
  runtimeId: "rt-a",
  ...over,
});

function wrapper() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }: { children: ReactNode }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

describe("mergeSwitchEvents", () => {
  const events = [msg("a", "2026-11-19T10:00:00Z"), msg("b", "2026-11-19T10:05:00Z"), msg("c", "2026-11-19T10:10:00Z")];

  it("returns the very same array without rows — the timeline is untouched", () => {
    expect(mergeSwitchEvents(events, [], "chat-1")).toBe(events);
    expect(mergeSwitchEvents(events, [row("s1", "2026-11-19T10:06:00Z")], null)).toBe(events);
  });

  it("puts each switch at its time, and only the newest offers switch back", () => {
    const out = mergeSwitchEvents(
      events,
      [row("s1", "2026-11-19T10:01:00+00:00", "failed"), row("s2", "2026-11-19T10:07:00Z", "failed")],
      "chat-1",
    );
    expect(out.map((e) => (e.kind === "message" ? e.uuid : e.kind))).toEqual(["a", "switch:s1", "b", "switch:s2", "c"]);
    const cards = out.filter((e) => e.kind === "message" && e.source?.kind === "switch");
    expect(cards.map((e) => (e.kind === "message" ? e.switchInfo?.latest : null))).toEqual([false, true]);
    expect(cards.every((e) => e.kind === "message" && e.role === "teammate" && e.switchInfo?.chatId === "chat-1")).toBe(true);
  });

  it("a switch newer than everything stands at the end", () => {
    const out = mergeSwitchEvents(events, [row("s9", "2026-11-19T11:00:00Z", "pending")], "chat-1");
    expect(out[out.length - 1]).toMatchObject({ uuid: "switch:s9" });
  });
});

describe("record helpers", () => {
  it("switchRowsOf keeps only kind=switch payloads, with the event id", () => {
    const rows = switchRowsOf([
      { id: "e1", seq: 1, event: { kind: "message", text: "hi" } },
      { id: "e2", seq: 2, event: { kind: "switch", status: "pending", path: "restart", ts: "2026-11-19T10:00:00Z" } },
      { id: "e3", seq: 3, event: null },
    ]);
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({ eventId: "e2", seq: 2, status: "pending" });
  });

  it("defaultRecordChat finds the agent's own chat only", () => {
    const chats = [chat({ id: "x", agentDefault: false }), chat({ id: "y", legacyAgentId: "other" }), chat({ id: "z" })];
    expect(defaultRecordChat(chats, "agent-1")?.id).toBe("z");
    expect(defaultRecordChat(chats, null)).toBeNull();
  });
});

describe("useChatSwitch", () => {
  it("record off (404): disabled, no history request, no rows", async () => {
    apiMock.listChats.mockRejectedValueOnce(new Error('API 404: {"detail":{"reason":"chat_record_disabled"}}'));
    apiMock.history.mockClear();
    const { result } = renderHook(() => useChatSwitch("agent-1"), { wrapper: wrapper() });
    await waitFor(() => expect(apiMock.listChats).toHaveBeenCalled());
    await new Promise((r) => setTimeout(r, 20));
    expect(result.current.enabled).toBe(false);
    expect(result.current.switches).toEqual([]);
    expect(apiMock.history).not.toHaveBeenCalled();
  });

  it("record on: reads the agent chat's switch rows and names the open one", async () => {
    apiMock.listChats.mockResolvedValueOnce({ chats: [chat()] });
    apiMock.history.mockResolvedValueOnce({
      events: [
        { id: "e1", seq: 1, sessionId: "s", event: { kind: "switch", status: "done", path: "restart", ts: "2026-11-19T10:00:00Z" } },
        { id: "e2", seq: 2, sessionId: "s", event: { kind: "switch", status: "pending", path: "restart", ts: "2026-11-19T10:05:00Z" } },
      ],
      hasMore: false,
    });
    const { result } = renderHook(() => useChatSwitch("agent-1"), { wrapper: wrapper() });
    await waitFor(() => expect(result.current.switches).toHaveLength(2));
    expect(result.current.enabled).toBe(true);
    expect(result.current.chat?.id).toBe("chat-1");
    expect(result.current.pending?.eventId).toBe("e2");
    expect(apiMock.history).toHaveBeenCalledWith("chat-1");
  });
});
