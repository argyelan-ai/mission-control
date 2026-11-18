/**
 * lib/chatList — the drawer's and the desktop column's chat list
 * (operator decision 2026-11-18): which chat lands in which section, in which
 * order, and what its row says.
 */
import { describe, expect, it } from "vitest";
import { buildChatList, chatRowTime, sectionChats, type ChatListInput } from "../chatList";
import type { HeadRun } from "../heads";

const NOW = new Date("2026-11-18T15:00:00");
const today = (h: number) => new Date(2026, 10, 18, h, 0).toISOString();
const daysAgo = (d: number) => new Date(2026, 10, 18 - d, 9, 0).toISOString();

function input(over: Partial<ChatListInput> = {}): ChatListInput {
  return {
    agents: [
      { id: "a1", name: "Lead", status: "idle", last_task_activity_at: today(9), updated_at: daysAgo(30) },
      { id: "a2", name: "Coder", status: "working", last_task_activity_at: daysAgo(2), updated_at: daysAgo(30) },
      { id: "a3", name: "Old", status: "archived", last_task_activity_at: today(10), updated_at: daysAgo(30) },
    ],
    groups: [{ id: "g1", name: "Review room", status: "idle", created_at: daysAgo(5), last_message: null }],
    headRuns: [],
    approvals: [],
    ...over,
  };
}

describe("buildChatList", () => {
  it("lists agents and groups newest first, never an archived agent", () => {
    const list = buildChatList(input());
    expect(list.map((e) => e.key)).toEqual(["agent:a1", "agent:a2", "group:g1"]);
  });

  it("a working agent is 'working', a quiet one 'idle'", () => {
    const list = buildChatList(input());
    expect(list.find((e) => e.id === "a2")?.state).toBe("working");
    expect(list.find((e) => e.id === "a1")?.state).toBe("idle");
  });

  it("a pending approval puts its agent in 'waiting'; a resolved one does not", () => {
    const list = buildChatList(
      input({ approvals: [{ agent_id: "a2", status: "pending" }, { agent_id: "a1", status: "approved" }] }),
    );
    expect(list.find((e) => e.id === "a2")?.state).toBe("waiting");
    expect(list.find((e) => e.id === "a1")?.state).toBe("idle");
  });

  it("a group at its gate waits for the operator", () => {
    const list = buildChatList(
      input({ groups: [{ id: "g1", name: "Room", status: "waiting_gate", created_at: daysAgo(1), last_message: null }] }),
    );
    expect(list.find((e) => e.id === "g1")?.state).toBe("waiting");
  });

  it("only heads with an open question are chats", () => {
    const runs = [
      { run_id: "r1", task_id: "t1", title: "Fix login", state: "needs_you", created_at: today(8), task_deleted: false },
      { run_id: "r2", task_id: "t2", title: "Done job", state: "passed", created_at: today(8), task_deleted: false },
    ] as unknown as HeadRun[];
    const heads = buildChatList(input({ headRuns: runs })).filter((e) => e.kind === "head");
    expect(heads.map((e) => [e.title, e.state, e.href])).toEqual([
      ["Fix login", "waiting", "/sessions?head=r1&enter=1"],
    ]);
  });

  // Operator pictures 18.11.2026: the phone had no Today section while the
  // desktop did — device-local data must not decide the grouping.
  it("sorts from server facts only — no device-local input exists", () => {
    const keys = Object.keys(input()).sort();
    expect(keys).toEqual(["agents", "approvals", "groups", "headRuns"]);
    expect(buildChatList(input()).find((e) => e.id === "a1")?.at).toBe(today(9));
  });

  it("a group's last message dates it", () => {
    const list = buildChatList(
      input({ groups: [{ id: "g1", name: "Room", status: "idle", created_at: daysAgo(5), last_message: { body: "x", sender: "y", created_at: today(14) } }] }),
    );
    expect(list[0].key).toBe("group:g1");
  });

  it("every row opens its chat from outside the list", () => {
    const list = buildChatList(input());
    expect(list.find((e) => e.id === "a1")?.href).toBe("/sessions?agent=a1&enter=1");
    expect(list.find((e) => e.id === "g1")?.href).toBe("/sessions?group=g1&enter=1");
  });
});

describe("sectionChats", () => {
  it("splits into Waiting for you / Today / Earlier", () => {
    const sections = sectionChats(
      buildChatList(input({ approvals: [{ agent_id: "a2", status: "pending" }] })),
      NOW,
    );
    expect(sections.waiting.map((e) => e.id)).toEqual(["a2"]);
    expect(sections.today.map((e) => e.id)).toEqual(["a1"]);
    expect(sections.earlier.map((e) => e.id)).toEqual(["g1"]);
  });

  it("a chat without any time is Earlier, not Today", () => {
    const sections = sectionChats(
      buildChatList(
        input({
          agents: [{ id: "x", name: "X", status: "idle", last_task_activity_at: null, updated_at: today(9) }],
          groups: [],
        }),
      ),
      NOW,
    );
    expect(sections.earlier.map((e) => e.id)).toEqual(["x"]);
    expect(sections.today).toEqual([]);
  });
});

describe("chatRowTime", () => {
  it("today: the clock time", () => {
    expect(chatRowTime(today(14), "de", NOW)).toBe("14:00");
  });
  it("before today: the date", () => {
    expect(chatRowTime(daysAgo(4), "de", NOW)).toBe("14.11.");
    expect(chatRowTime(daysAgo(4), "en", NOW)).toBe("Nov 14");
  });
  it("no time, no text", () => {
    expect(chatRowTime(null, "en", NOW)).toBeNull();
  });
});

describe("chatListPollMs (shell review E1)", () => {
  it("polls as before on the chat page, calmly everywhere else", async () => {
    const { chatListPollMs } = await import("@/components/layout/useChatList");
    expect(chatListPollMs("/sessions")).toBe(10_000);
    expect(chatListPollMs("/runtimes")).toBe(60_000);
    expect(chatListPollMs("/settings")).toBe(60_000);
  });
});
