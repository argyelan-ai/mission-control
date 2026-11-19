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

describe("recordPollMs (critic 19.11.)", () => {
  it("stops asking the record after its 404 (flag off), keeps polling on other failures", async () => {
    const { recordPollMs } = await import("@/components/layout/useChatList");
    expect(recordPollMs(new Error('API 404: {"detail":"chat_record_disabled"}'), 60_000)).toBe(false);
    expect(recordPollMs(new Error("API 502: bad gateway"), 60_000)).toBe(60_000);
    expect(recordPollMs(null, 10_000)).toBe(10_000);
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

describe("buildChatList — further chats of an agent (chats slice S2)", () => {
  const rec = (over: Record<string, unknown>) => ({
    id: "c-x", title: "Lead", harness: "omp", legacyAgentId: "a1", agentDefault: false, archived: false,
    lastActivityAt: today(11), createdAt: null, ...over,
  });

  it("without record chats the list is exactly the one before", () => {
    expect(buildChatList(input({ recordChats: [] }))).toEqual(buildChatList(input()));
  });

  it("a further chat of the same agent is its own row, numbered from 2, linking to that chat", () => {
    const list = buildChatList(input({
      recordChats: [
        rec({ id: "d-1", agentDefault: true }),
        rec({ id: "c-2", lastActivityAt: today(12), createdAt: today(1) }),
        rec({ id: "c-3", lastActivityAt: today(8), createdAt: today(2) }),
      ],
    }));
    const rows = list.filter((e) => e.id === "a1");
    expect(rows.map((e) => e.key)).toEqual(["chat:c-2", "agent:a1", "chat:c-3"]);
    const second = rows[0];
    expect(second.ordinal).toBe(2);
    expect(second.title).toBe("Lead");
    expect(second.chatId).toBe("c-2");
    expect(second.href).toBe("/sessions?agent=a1&enter=1&chat=c-2");
    expect(rows[2].ordinal).toBe(3);
    // the default chat IS the agent row — never a second one
    expect(list.some((e) => e.key === "chat:d-1")).toBe(false);
  });

  it("the numbers follow creation, so writing in one chat renames none (critic 19.11.)", () => {
    const b = rec({ id: "c-b", createdAt: today(1) });
    const a = rec({ id: "c-a", createdAt: today(2) });
    // the record lists by activity: B was active last, then A is
    const before = buildChatList(input({ recordChats: [{ ...b, lastActivityAt: today(10) }, { ...a, lastActivityAt: today(9) }] }));
    const after = buildChatList(input({ recordChats: [{ ...a, lastActivityAt: today(11) }, { ...b, lastActivityAt: today(10) }] }));
    const n = (list: ReturnType<typeof buildChatList>, id: string) => list.find((e) => e.chatId === id)?.ordinal;
    expect([n(before, "c-b"), n(before, "c-a")]).toEqual([2, 3]);
    expect([n(after, "c-b"), n(after, "c-a")]).toEqual([2, 3]);
  });

  it("only harnesses whose daemon holds further chats get rows (omp)", () => {
    const list = buildChatList(input({
      recordChats: [rec({ id: "c-h", harness: "hermes" }), rec({ id: "c-c", harness: "claude-code" }), rec({ id: "c-o" })],
    }));
    expect(list.filter((e) => e.key.startsWith("chat:")).map((e) => e.chatId)).toEqual(["c-o"]);
  });

  it("a chat with its own title keeps it and gets no number", () => {
    const [row] = buildChatList(input({ recordChats: [rec({ id: "c-9", title: "Refactor" })] }))
      .filter((e) => e.key === "chat:c-9");
    expect(row.title).toBe("Refactor");
    expect(row.ordinal).toBeUndefined();
  });

  it("archived chats and chats of an unlisted or archived agent show nothing", () => {
    const list = buildChatList(input({
      recordChats: [
        rec({ id: "c-a", archived: true }),
        rec({ id: "c-b", legacyAgentId: "a3" }),
        rec({ id: "c-c", legacyAgentId: "nobody" }),
        rec({ id: "c-d", legacyAgentId: null }),
      ],
    }));
    expect(list.filter((e) => e.key.startsWith("chat:"))).toEqual([]);
  });

  it("a further chat claims no state of its own — the agent's state is its own chat's", () => {
    const [row] = buildChatList(input({
      recordChats: [rec({ id: "c-w", legacyAgentId: "a2", title: "Coder" })],
      approvals: [{ agent_id: "a2", status: "pending" }],
    })).filter((e) => e.key === "chat:c-w");
    expect(row.state).toBe("idle");
  });
});
