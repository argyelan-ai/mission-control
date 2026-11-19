/**
 * useRecordChat — a further chat read from the chat record (chats slice S2).
 * `request` is mocked: what the hook asks, what it makes of the answer, and
 * the 404 that means "record off → show the agent's own chat".
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { RecordChat } from "@/lib/api";
import type { MessageEvent } from "@/lib/chatTypes";
import {
  furtherChatHref,
  furtherChatLabel,
  newChatIdOf,
  RECORD_CHAT_FAST_POLL_MS,
  sendToChat,
  useRecordChat,
} from "./useRecordChat";

const requestMock = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api", () => ({ request: requestMock }));

function msg(uuid: string, role: MessageEvent["role"], text: string): MessageEvent {
  return { kind: "message", uuid, ts: "2026-11-19T10:00:00Z", role, text, model: null, sidechain: false };
}
function page(events: MessageEvent[]) {
  return { events: events.map((e, i) => ({ id: `ev-${e.uuid}`, seq: i, sessionId: "s", event: e })), hasMore: false };
}

type Answer = unknown | Error;
function answer(history: () => Answer, chats: RecordChat[] = []) {
  requestMock.mockImplementation(async (path: string) => {
    if (path === "/api/v1/chat-record/chats") return { chats };
    if (path.includes("/history")) {
      const a = history();
      if (a instanceof Error) throw a;
      return a;
    }
    return undefined;
  });
}

beforeEach(() => {
  requestMock.mockReset();
});
afterEach(() => vi.useRealTimers());

describe("useRecordChat", () => {
  it("asks nothing without a chat id", () => {
    const { result } = renderHook(() => useRecordChat(null));
    expect(requestMock).not.toHaveBeenCalled();
    expect(result.current.disabled).toBe(false);
  });

  it("loads the chat's history from the record", async () => {
    answer(() => page([msg("a", "user", "hi"), msg("b", "assistant", "hello back")]));
    const { result } = renderHook(() => useRecordChat("chat-2"));
    await waitFor(() => expect(result.current.events).toHaveLength(2));
    expect(requestMock).toHaveBeenCalledWith("/api/v1/chat-record/chats/chat-2/history?limit=500");
    expect(result.current.loading).toBe(false);
    expect(result.current.disabled).toBe(false);
    // Read and quiet = ready, never "unknown".
    expect(result.current.state?.status).toBe("idle");
  });

  it("404 (record off, or no such chat) → disabled", async () => {
    answer(() => new Error('API 404: {"detail":"chat_record_disabled"}'));
    const { result } = renderHook(() => useRecordChat("chat-2"));
    await waitFor(() => expect(result.current.disabled).toBe(true));
  });

  it("another failure is not 'off' — the chat stays a further chat", async () => {
    answer(() => new Error("API 500: boom"));
    const { result } = renderHook(() => useRecordChat("chat-2"));
    await waitFor(() => expect(result.current.error).not.toBeNull());
    expect(result.current.disabled).toBe(false);
  });

  it("an echo stays until its message is in the record, then the answer ends 'sent'", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let events = [msg("a", "user", "old")];
    answer(() => page(events));
    const { result } = renderHook(() => useRecordChat("chat-2"));
    await waitFor(() => expect(result.current.events).toHaveLength(1));
    act(() => result.current.echoSent("new question"));
    expect(result.current.pendingEchoes.map((e) => e.text)).toEqual(["new question"]);
    expect(result.current.awaitingResponse).toBe(true);

    events = [...events, msg("b", "user", "new question")];
    await act(async () => {
      await vi.advanceTimersByTimeAsync(RECORD_CHAT_FAST_POLL_MS + 10);
    });
    await waitFor(() => expect(result.current.pendingEchoes).toHaveLength(0));
    expect(result.current.awaitingResponse).toBe(true);

    events = [...events, msg("c", "assistant", "the answer")];
    await act(async () => {
      await vi.advanceTimersByTimeAsync(RECORD_CHAT_FAST_POLL_MS + 10);
    });
    await waitFor(() => expect(result.current.awaitingResponse).toBe(false));
    expect(result.current.events).toHaveLength(3);
  });

  it("the first page never retires an echo (old text that happens to match)", async () => {
    answer(() => page([msg("a", "user", "ok")]));
    const { result } = renderHook(() => useRecordChat("chat-2"));
    await waitFor(() => expect(result.current.events).toHaveLength(1));
    act(() => result.current.echoSent("ok"));
    expect(result.current.pendingEchoes).toHaveLength(1);
  });
});

describe("further chat helpers", () => {
  it("furtherChatHref names agent and chat", () => {
    expect(furtherChatHref("a 1", "c/2")).toBe("/sessions?agent=a%201&chat=c%2F2");
  });

  it("newChatIdOf reads the input route's 201 body only", () => {
    expect(newChatIdOf({ newChat: { id: "chat-3", title: "Cody" } })).toBe("chat-3");
    expect(newChatIdOf(undefined)).toBeNull();
    expect(newChatIdOf({})).toBeNull();
    expect(newChatIdOf({ newChat: { id: "" } })).toBeNull();
  });

  it("sendToChat posts the text with chatId to the agent's input route", async () => {
    requestMock.mockResolvedValue(undefined);
    await sendToChat("agent-1", "chat-2", "a\r\nb");
    expect(requestMock).toHaveBeenCalledWith("/api/v1/agents/agent-1/chat/input", {
      method: "POST",
      body: JSON.stringify({ text: "a\nb", chatId: "chat-2" }),
    });
  });

  it("furtherChatLabel numbers like the chat list (created order, agent row = 1)", () => {
    const mk = (id: string, createdAt: string, title = "Cody"): RecordChat => ({
      id, title, harness: "omp", model: null, legacyAgentId: "agent-1", agentDefault: false,
      nativeSessionId: null, pinned: false, archived: false, lastActivityAt: null, createdAt,
    });
    const chats = [mk("late", "2026-11-19T12:00:00Z"), mk("early", "2026-11-19T08:00:00Z"), mk("named", "2026-11-19T13:00:00Z", "Refactor")];
    const fmt = (name: string, n: number) => `${name} · chat ${n}`;
    const agent = { id: "agent-1", name: "Cody" };
    expect(furtherChatLabel(chats, agent, "early", fmt)).toBe("Cody · chat 2");
    expect(furtherChatLabel(chats, agent, "late", fmt)).toBe("Cody · chat 3");
    expect(furtherChatLabel(chats, agent, "named", fmt)).toBe("Refactor");
    expect(furtherChatLabel(chats, agent, "missing", fmt)).toBeNull();
  });
});
