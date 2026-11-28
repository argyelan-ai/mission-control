import { describe, expect, it } from "vitest";
import type { ChatListEntry } from "@/lib/chatList";
import {
  SCORE_EXACT,
  SCORE_FUZZY,
  SCORE_INSIDE,
  SCORE_WORD_START,
  matchScore,
  messageHitTarget,
  rankByName,
  rankChats,
  recentChats,
  splitHighlight,
  wantsMessageSearch,
} from "../paletteSearch";

const entry = (key: string, title: string, at: string | null, extra: Partial<ChatListEntry> = {}): ChatListEntry => {
  const [kind, id] = key.split(":");
  return {
    key,
    kind: kind === "group" ? "group" : "agent",
    id,
    title,
    at,
    state: "idle",
    href: `/sessions?agent=${id}&from=link`,
    ...extra,
  };
};

describe("matchScore — exact > word start > inside > fuzzy", () => {
  it("scores the four levels", () => {
    expect(matchScore("alpha", "Alpha")).toBe(SCORE_EXACT);
    expect(matchScore("kühl", "Der Kühlstand")).toBe(SCORE_WORD_START);
    expect(matchScore("stand", "Der Kühlstand")).toBe(SCORE_INSIDE);
    expect(matchScore("kst", "Kühlstand")).toBe(SCORE_FUZZY);
    expect(matchScore("xyz", "Kühlstand")).toBeNull();
  });

  it("folds umlauts and case", () => {
    expect(matchScore("kuhl", "Kühlstand")).toBe(SCORE_WORD_START);
  });

  it("no fuzzy letter soup under three letters", () => {
    expect(matchScore("ks", "Kühlstand")).toBeNull();
  });

  it("every query word must match", () => {
    expect(matchScore("deploy plan", "Deploy plan for Friday")).toBe(SCORE_WORD_START);
    expect(matchScore("deploy zebra", "Deploy plan for Friday")).toBeNull();
  });
});

describe("rankChats", () => {
  const titleOf = (e: ChatListEntry) => e.title;
  const noAgent = () => null;

  it("word-start title before fuzzy title, then recency", () => {
    const rows = [
      entry("agent:a", "Planner", "2026-11-28T10:00:00Z"),
      entry("agent:b", "Alpha", "2026-11-28T09:00:00Z"),
      entry("agent:c", "Alpha notes", "2026-11-28T11:00:00Z"),
      entry("agent:d", "Al-P-Ha-Xy", "2026-11-28T12:00:00Z"),
    ];
    const ranked = rankChats(rows, "alph", titleOf, noAgent).map((e) => e.key);
    // both word-start → newest first; fuzzy (A-l-P-H-a) last
    expect(ranked).toEqual(["agent:c", "agent:b", "agent:d"]);
  });

  it("an exact title beats a newer word-start one", () => {
    const rows = [entry("agent:a", "Alpha two", "2026-11-28T12:00:00Z"), entry("agent:b", "Alpha", "2026-11-01T09:00:00Z")];
    expect(rankChats(rows, "alpha", titleOf, noAgent)[0].key).toBe("agent:b");
  });

  it("finds a titled further chat by its agent's name, below a title hit", () => {
    const rows = [
      entry("chat:c1", "Cooling stand", "2026-11-28T12:00:00Z", { chatId: "c1" }),
      entry("agent:a", "Alpha", "2026-11-01T09:00:00Z"),
    ];
    const ranked = rankChats(rows, "alph", titleOf, (e) => (e.key === "chat:c1" ? "Alpha" : null));
    expect(ranked.map((e) => e.key)).toEqual(["agent:a", "chat:c1"]);
  });
});

describe("rankByName — commands and pages", () => {
  it("names only, no fuzzy soup", () => {
    const pages = [{ n: ["Settings", "Einstellungen"] }, { n: ["Sessions"] }, { n: ["Runtimes"] }];
    expect(rankByName(pages, "s", (p) => p.n).length).toBe(2);
    expect(rankByName(pages, "einst", (p) => p.n)).toEqual([pages[0]]);
    expect(rankByName(pages, "rnts", (p) => p.n)).toEqual([]);
  });
});

describe("recentChats — pinned first, at most 8", () => {
  it("puts pinned rows (record pin) on top, agent default chat → agent row", () => {
    const rows = Array.from({ length: 10 }, (_, i) => entry(`agent:a${i}`, `A${i}`, null));
    rows.push(entry("chat:c9", "Further", null, { chatId: "c9" }));
    const out = recentChats(rows, [
      { id: "c9", agentDefault: false, legacyAgentId: "a1", pinned: true },
      { id: "d5", agentDefault: true, legacyAgentId: "a5", pinned: true },
    ]);
    expect(out).toHaveLength(8);
    expect(out.slice(0, 2).map((e) => e.key)).toEqual(["agent:a5", "chat:c9"]);
  });
});

describe("messageHitTarget", () => {
  const rows = [entry("agent:a1", "Alpha", null), entry("chat:c2", "Alpha", null, { chatId: "c2", href: "/sessions?agent=a1&from=link&chat=c2" })];

  it("a further chat opens its own row", () => {
    expect(messageHitTarget({ chatId: "c2", agentId: "a1" }, rows, [])?.href).toContain("chat=c2");
  });

  it("the agent's own chat opens the agent row", () => {
    const t = messageHitTarget({ chatId: "d1", agentId: "a1" }, rows, [{ id: "d1", agentDefault: true, legacyAgentId: "a1" }]);
    expect(t?.key).toBe("agent:a1");
    expect(t?.href).not.toContain("chat=");
  });

  it("a further chat the list has no row for yet still opens by id", () => {
    const t = messageHitTarget({ chatId: "c3", agentId: "a1" }, rows, [{ id: "c3", agentDefault: false, legacyAgentId: "a1" }]);
    expect(t?.href).toContain("chat=c3");
  });

  it("an agent the list does not show leads nowhere", () => {
    expect(messageHitTarget({ chatId: "x", agentId: "gone" }, rows, [])).toBeNull();
  });
});

describe("splitHighlight", () => {
  it("cuts at UTF-16 indices", () => {
    expect(splitHighlight("…the 🚀 Kühlstand is", [8, 17])).toEqual({ before: "…the 🚀 ", match: "Kühlstand", after: " is" });
  });
  it("a bad range highlights nothing", () => {
    expect(splitHighlight("abc", [2, 9]).match).toBe("");
    expect(splitHighlight("abc", null).before).toBe("abc");
  });
});

it("server search from three characters on", () => {
  expect(wantsMessageSearch(" ab ")).toBe(false);
  expect(wantsMessageSearch("abc")).toBe(true);
});
