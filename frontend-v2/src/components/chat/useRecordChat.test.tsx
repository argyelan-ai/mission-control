/**
 * useRecordChat — a further chat read from the chat record (chats slice S2).
 * `request` is mocked: what the hook asks, what it makes of the answer, and
 * the 404 that means "record off → show the agent's own chat".
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { RecordChat } from "@/lib/api";
import type { ChatEvent, MessageEvent, ToolEvent } from "@/lib/chatTypes";
import { ECHO_CONFIRM_TIMEOUT_MS } from "@/hooks/useChatStream";
import {
  furtherChatHref,
  furtherChatLabel,
  newChatIdOf,
  RECORD_CHAT_AWAIT_CEILING_MS,
  RECORD_CHAT_FAST_POLL_MS,
  RECORD_CHAT_POLL_MS,
  RECORD_CHAT_POLL_PAGE,
  sendToChat,
  useRecordChat,
} from "./useRecordChat";

const requestMock = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api", () => ({ request: requestMock }));

function msg(uuid: string, role: MessageEvent["role"], text: string): MessageEvent {
  return { kind: "message", uuid, ts: "2026-11-19T10:00:00Z", role, text, model: null, sidechain: false };
}
function tool(uuid: string, status: ToolEvent["status"] = "error"): ToolEvent {
  return {
    kind: "tool", uuid, ts: "2026-11-19T10:00:01Z", name: "bash", title: "bash", detail: {},
    toolUseId: uuid, result: "failed", status, stats: null, sidechain: false,
  };
}
function page(events: ChatEvent[], hasMore = false) {
  return {
    events: events.map((e, i) => ({ id: `ev-${(e as MessageEvent).uuid}`, seq: i, sessionId: "s", event: e })),
    hasMore,
  };
}
const historyCalls = () =>
  requestMock.mock.calls.map((c) => c[0] as string).filter((p) => p.includes("/history"));

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

  it("404 chat_record_disabled (record off) → disabled", async () => {
    answer(() => new Error('API 404: {"detail":{"reason":"chat_record_disabled"}}'));
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

  it("a turn that ends with only a tool card (no assistant text) still ends 'sent'", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let events: ChatEvent[] = [msg("a", "user", "old")];
    answer(() => page(events));
    const { result } = renderHook(() => useRecordChat("chat-2"));
    await waitFor(() => expect(result.current.events).toHaveLength(1));
    act(() => result.current.echoSent("do it"));
    events = [...events, msg("b", "user", "do it"), tool("t1")];
    await act(async () => {
      await vi.advanceTimersByTimeAsync(RECORD_CHAT_FAST_POLL_MS + 10);
    });
    await waitFor(() => expect(result.current.awaitingResponse).toBe(false));
    expect(result.current.pendingEchoes).toHaveLength(0);
  });

  it("'sent' never stays up for good: a hard ceiling ends it when nothing comes", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    answer(() => page([msg("a", "user", "old")]));
    const { result } = renderHook(() => useRecordChat("chat-2"));
    await waitFor(() => expect(result.current.events).toHaveLength(1));
    act(() => result.current.echoSent("hello"));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(RECORD_CHAT_AWAIT_CEILING_MS - 1_000);
    });
    expect(result.current.awaitingResponse).toBe(true);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
    });
    expect(result.current.awaitingResponse).toBe(false);
  });

  it("an echo the record never confirms turns 'unconfirmed' like in the agent's own chat, and the fast poll ends", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    answer(() => page([msg("a", "user", "old")]));
    const { result } = renderHook(() => useRecordChat("chat-2"));
    await waitFor(() => expect(result.current.events).toHaveLength(1));
    act(() => result.current.echoSent("hello"));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(ECHO_CONFIRM_TIMEOUT_MS + 2_000);
    });
    expect(result.current.pendingEchoes[0]?.status).toBe("unconfirmed");
    // Past the ceiling nothing is pending any more → back to the quiet poll.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(RECORD_CHAT_AWAIT_CEILING_MS);
    });
    const before = historyCalls().length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(RECORD_CHAT_POLL_MS * 2 + 10);
    });
    expect(historyCalls().length - before).toBe(2);
  });

  it("after the first read the poll asks only a small page, merged by event id", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let events: ChatEvent[] = [msg("a", "user", "hi"), msg("b", "assistant", "hello")];
    answer(() => page(events));
    const { result } = renderHook(() => useRecordChat("chat-2"));
    await waitFor(() => expect(result.current.events).toHaveLength(2));
    events = [...events, msg("c", "assistant", "more")];
    await act(async () => {
      await vi.advanceTimersByTimeAsync(RECORD_CHAT_POLL_MS + 10);
    });
    await waitFor(() => expect(result.current.events).toHaveLength(3));
    const calls = historyCalls();
    expect(calls[0]).toBe("/api/v1/chat-record/chats/chat-2/history?limit=500");
    expect(calls[calls.length - 1]).toBe(`/api/v1/chat-record/chats/chat-2/history?limit=${RECORD_CHAT_POLL_PAGE}`);
  });

  it("a small page with nothing known in it (a gap) reads the full page again", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let full = page([msg("a", "user", "hi")]);
    let small = full;
    requestMock.mockImplementation(async (path: string) => {
      if (path === "/api/v1/chat-record/chats") return { chats: [] };
      if (path.endsWith(`limit=${RECORD_CHAT_POLL_PAGE}`)) return small;
      if (path.includes("/history")) return full;
      return undefined;
    });
    const { result } = renderHook(() => useRecordChat("chat-2"));
    await waitFor(() => expect(result.current.events).toHaveLength(1));
    full = page([msg("a", "user", "hi"), msg("g1", "assistant", "gap 1"), msg("g2", "assistant", "gap 2")]);
    small = page([msg("g2", "assistant", "gap 2")], true);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(RECORD_CHAT_POLL_MS + 10);
    });
    await waitFor(() => expect(result.current.events).toHaveLength(3));
  });

  it("a send before the first read lands is still matched against that read", async () => {
    let release: (v: unknown) => void = () => {};
    requestMock.mockImplementation((path: string) => {
      if (path === "/api/v1/chat-record/chats") return Promise.resolve({ chats: [] });
      return new Promise((resolve) => {
        release = resolve;
      });
    });
    const { result } = renderHook(() => useRecordChat("chat-2"));
    act(() => result.current.echoSent("quick"));
    const now = new Date().toISOString();
    await act(async () => {
      release(page([{ ...msg("a", "user", "quick"), ts: now }, { ...msg("b", "assistant", "ok"), ts: now }]));
    });
    await waitFor(() => expect(result.current.events).toHaveLength(2));
    expect(result.current.pendingEchoes).toHaveLength(0);
    expect(result.current.awaitingResponse).toBe(false);
  });

  it("…but an old line of the same text in that first read does not claim the send", async () => {
    let release: (v: unknown) => void = () => {};
    requestMock.mockImplementation((path: string) => {
      if (path === "/api/v1/chat-record/chats") return Promise.resolve({ chats: [] });
      return new Promise((resolve) => {
        release = resolve;
      });
    });
    const { result } = renderHook(() => useRecordChat("chat-2"));
    act(() => result.current.echoSent("ok"));
    await act(async () => {
      // Hours old: the fixture's 10:00 stamp, long before this send.
      release(page([{ ...msg("a", "user", "ok"), ts: "2020-01-01T10:00:00Z" }, msg("b", "assistant", "fine")]));
    });
    await waitFor(() => expect(result.current.events).toHaveLength(2));
    expect(result.current.pendingEchoes).toHaveLength(1);
    expect(result.current.awaitingResponse).toBe(true);
  });

  it("'Chat not found' is not 'record off': missing, not disabled", async () => {
    answer(() => new Error('API 404: {"detail":"Chat not found"}'));
    const { result } = renderHook(() => useRecordChat("chat-2"));
    await waitFor(() => expect(result.current.missing).toBe(true));
    expect(result.current.disabled).toBe(false);
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
