/**
 * Sidebar "Agents" badge — same truth as the /agents header.
 *
 * The badge read "14/14" while 12 of 14 agents were paused: it showed
 * metrics.agents.online, and a paused agent still heartbeats as "idle".
 * It now shows active/total with the /agents label as tooltip.
 */
import { describe, it, expect } from "vitest";
import en from "../../../../messages/en.json";
import de from "../../../../messages/de.json";
import { agentsBadge } from "../agentsBadge";

const tr = (dict: Record<string, string>) => (key: string, v?: Record<string, unknown>) => {
  let s = dict[key] ?? key;
  for (const [k, val] of Object.entries(v ?? {})) s = s.split(`{${k}}`).join(String(val));
  return s;
};
const tEn = tr(en.agents as unknown as Record<string, string>);
const tDe = tr(de.agents as unknown as Record<string, string>);

describe("agentsBadge", () => {
  it("counts only active agents, paused ones go to the tooltip", () => {
    const b = agentsBadge({ total: 14, active: 2, paused: 12, online: 14 }, tEn);
    expect(b).toEqual({ text: "2/14", title: "2 active · 12 paused" });
  });

  it("drops the paused part when nothing is paused", () => {
    expect(agentsBadge({ total: 3, active: 3, paused: 0, online: 1 }, tEn)).toEqual({
      text: "3/3",
      title: "3 active",
    });
  });

  it("is translated, never hard-coded", () => {
    expect(agentsBadge({ total: 14, active: 2, paused: 12, online: 14 }, tDe).title).toBe(
      "2 aktiv · 12 pausiert",
    );
  });

  it("falls back to online when an older backend sends no split", () => {
    expect(agentsBadge({ total: 4, online: 1 }, tEn)).toEqual({ text: "1/4" });
  });
});

describe("Sidebar", () => {
  it("renders the /agents badge through agentsBadge, not the online counter", async () => {
    const { readFileSync } = await import("node:fs");
    const { join } = await import("node:path");
    const src = readFileSync(join(__dirname, "../Sidebar.tsx"), "utf-8");
    expect(src).toContain("agentsBadge(metrics.agents");
    expect(src).not.toContain("metrics.agents.online}/");
  });
});
