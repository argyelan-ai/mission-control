/**
 * /agents header count — paused agents are not "online".
 *
 * The header read "14/14 online" while 12 of 14 agents were paused: idle
 * counted as online and operational_mode was ignored. It now says
 * "2 active · 12 paused".
 */
import { describe, it, expect } from "vitest";
import en from "../../../../messages/en.json";
import { fleetCount, fleetCountLabel } from "../fleetCount";
import type { Agent } from "@/lib/types";

const agent = (mode: "active" | "paused", status = "idle") =>
  ({ id: Math.random().toString(), operational_mode: mode, status }) as unknown as Agent;

const t = (key: string, v?: Record<string, unknown>) => {
  let s = (en.agents as unknown as Record<string, string>)[key] ?? key;
  for (const [k, val] of Object.entries(v ?? {})) s = s.split(`{${k}}`).join(String(val));
  return s;
};

describe("fleetCount", () => {
  it("splits the fleet by operational mode", () => {
    const agents = [agent("active"), agent("active", "busy"), ...Array.from({ length: 12 }, () => agent("paused"))];
    expect(fleetCount(agents)).toEqual({ active: 2, paused: 12 });
    expect(fleetCountLabel(fleetCount(agents), t)).toBe("2 active · 12 paused");
  });

  it("leaves the paused part out when nothing is paused", () => {
    const agents = [agent("active"), agent("active")];
    expect(fleetCountLabel(fleetCount(agents), t)).toBe("2 active");
  });

  it("copes with a list that has not loaded yet", () => {
    expect(fleetCount(undefined)).toEqual({ active: 0, paused: 0 });
  });
});

describe("/agents page header", () => {
  it("uses the mode-aware label, not the old online counter", async () => {
    const { readFileSync } = await import("node:fs");
    const { join } = await import("node:path");
    const src = readFileSync(join(__dirname, "../page.tsx"), "utf-8");
    expect(src).toContain("fleetCountLabel(");
    expect(src).not.toContain('t("onlineCount"');
  });
});
