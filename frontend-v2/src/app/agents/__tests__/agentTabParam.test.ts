/**
 * Agent detail tab ↔ URL.
 *
 * The roster's ⋯ menu linked to `/agents/<id>?tab=config`, but the detail
 * page ignored `?tab=` and always opened "Overview"; a second entry pointed at
 * an "analytics" tab that does not exist.
 */
import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { AGENT_TABS, agentTabFromParam, agentTabHref } from "../agentTabParam";

describe("agentTabFromParam", () => {
  it.each(AGENT_TABS)("accepts the real tab %s", (tab) => {
    expect(agentTabFromParam(tab)).toBe(tab);
  });

  it("falls back to overview for missing or unknown tabs", () => {
    expect(agentTabFromParam(null)).toBe("overview");
    expect(agentTabFromParam("analytics")).toBe("overview");
  });

  it("builds a clean URL (no ?tab for the default tab)", () => {
    expect(agentTabHref("a1", "overview")).toBe("/agents/a1");
    expect(agentTabHref("a1", "config")).toBe("/agents/a1?tab=config");
  });
});

describe("wiring", () => {
  const detail = readFileSync(join(__dirname, "../[id]/page.tsx"), "utf-8");
  const roster = readFileSync(join(__dirname, "../page.tsx"), "utf-8");

  it("the detail page reads the tab from the URL and writes it back", () => {
    expect(detail).toMatch(/agentTabFromParam\(searchParams\.get\("tab"\)\)/);
    expect(detail).toContain("agentTabHref(");
  });

  it("the roster menu no longer links to a non-existent analytics tab", () => {
    expect(roster).not.toContain("tab=analytics");
  });
});
