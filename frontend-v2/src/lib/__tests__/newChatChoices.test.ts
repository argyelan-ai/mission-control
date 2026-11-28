/**
 * lib/chatList — who the "New chat" draft's agent chip offers (concept
 * "Neuer Chat", P1 + P4): alive agents that can hold a further chat on top,
 * alive agents with only their own chat dimmed below, nothing dead.
 */
import { describe, expect, it } from "vitest";
import {
  NEW_CHAT_HOST_ALIVE_MS,
  defaultNewChatAgent,
  isAgentAliveForNewChat,
  newChatChoices,
  type NewChatAgent,
} from "../chatList";

const NOW = new Date("2026-11-28T12:00:00Z");
const ago = (ms: number) => new Date(NOW.getTime() - ms).toISOString();
const H = 60 * 60 * 1000;

function agent(over: Partial<NewChatAgent> & { id: string }): NewChatAgent {
  return {
    name: over.id,
    status: "idle",
    agent_runtime: "cli-bridge",
    container_state: "running",
    last_seen_at: ago(5_000),
    can_start_chat: false,
    ...over,
  };
}

describe("isAgentAliveForNewChat", () => {
  it("a host agent counts as alive for 24 h after its last heartbeat", () => {
    expect(isAgentAliveForNewChat(agent({ id: "h", agent_runtime: "host", container_state: undefined, last_seen_at: ago(23 * H) }), NOW)).toBe(true);
    expect(isAgentAliveForNewChat(agent({ id: "h", agent_runtime: "host", container_state: undefined, last_seen_at: ago(NEW_CHAT_HOST_ALIVE_MS + 1) }), NOW)).toBe(false);
    expect(isAgentAliveForNewChat(agent({ id: "h", agent_runtime: "host", container_state: undefined, last_seen_at: null }), NOW)).toBe(false);
  });

  it("a container agent is alive only while its container runs", () => {
    expect(isAgentAliveForNewChat(agent({ id: "d" }), NOW)).toBe(true);
    expect(isAgentAliveForNewChat(agent({ id: "d", container_state: "exited" }), NOW)).toBe(false);
  });

  it("a container agent without a container fact falls back to its status", () => {
    expect(isAgentAliveForNewChat(agent({ id: "d", container_state: undefined, status: "offline" }), NOW)).toBe(false);
    expect(isAgentAliveForNewChat(agent({ id: "d", container_state: undefined, status: "idle" }), NOW)).toBe(true);
  });

  it("an archived agent is never alive", () => {
    expect(isAgentAliveForNewChat(agent({ id: "x", status: "archived" }), NOW)).toBe(false);
  });
});

describe("newChatChoices", () => {
  const alpha = agent({ id: "alpha", can_start_chat: true });
  const lead = agent({ id: "lead", agent_runtime: "host", container_state: undefined, last_seen_at: ago(30_000) });
  const deadTest = agent({ id: "mcp-test", agent_runtime: "host", container_state: undefined, last_seen_at: ago(10 * 24 * H) });
  const stopped = agent({ id: "rex", container_state: "exited", can_start_chat: true });
  const archived = agent({ id: "old", status: "archived", can_start_chat: true });

  it("splits alive agents into 'new chat' and 'only one chat', drops dead and archived", () => {
    const c = newChatChoices([lead, alpha, deadTest, stopped, archived], { newChatsOn: true }, NOW);
    expect(c.canStart.map((a) => a.id)).toEqual(["alpha"]);
    expect(c.onlyOne.map((a) => a.id)).toEqual(["lead"]);
  });

  it("never offers a host agent a further chat, even if a row claims it could", () => {
    const hostClaim = agent({ id: "hermes", agent_runtime: "host", container_state: undefined, can_start_chat: true });
    const c = newChatChoices([hostClaim], { newChatsOn: true }, NOW);
    expect(c.canStart).toEqual([]);
    expect(c.onlyOne.map((a) => a.id)).toEqual(["hermes"]);
  });

  it("with new chats off (flag or no admin) every alive agent has only its own chat", () => {
    const c = newChatChoices([lead, alpha, deadTest], { newChatsOn: false }, NOW);
    expect(c.canStart).toEqual([]);
    expect(c.onlyOne.map((a) => a.id)).toEqual(["lead", "alpha"]);
  });
});

describe("defaultNewChatAgent", () => {
  const a = agent({ id: "a", can_start_chat: true });
  const b = agent({ id: "b", can_start_chat: true });
  const lead = agent({ id: "lead", agent_runtime: "host", container_state: undefined });

  it("takes the last used agent that can start a chat", () => {
    expect(defaultNewChatAgent({ canStart: [a, b], onlyOne: [lead] }, ["lead", "b", "a"])?.id).toBe("b");
  });

  it("falls back to the first agent that can start a chat", () => {
    expect(defaultNewChatAgent({ canStart: [a, b], onlyOne: [lead] }, ["lead"])?.id).toBe("a");
  });

  it("with no agent that can start a chat, the last used alive agent (its own chat)", () => {
    const c = agent({ id: "c" });
    expect(defaultNewChatAgent({ canStart: [], onlyOne: [lead, c] }, ["c"])?.id).toBe("c");
    expect(defaultNewChatAgent({ canStart: [], onlyOne: [lead, c] }, [])?.id).toBe("lead");
  });

  it("nobody alive: null", () => {
    expect(defaultNewChatAgent({ canStart: [], onlyOne: [] }, ["a"])).toBeNull();
  });
});
