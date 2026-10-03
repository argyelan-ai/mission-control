/**
 * sheetPages — level 2 of the phone menu is generated from lib/nav.ts.
 * A page added to NAV_ITEMS appears; a switched-off vertical (Benchmark)
 * disappears exactly like in the sidebar; tabs and Settings never repeat.
 */
import { describe, expect, it } from "vitest";
import { Server } from "lucide-react";
import { NAV_ITEMS, type NavItem } from "../nav";
import { sheetPages, TAB_HREFS } from "../mobileNav";

const hrefs = (items: NavItem[]) => items.map((i) => i.href);

describe("sheetPages", () => {
  it("covers every NAV_ITEM that is neither a tab nor Settings", () => {
    const { usedMost, others } = sheetPages();
    const expected = NAV_ITEMS.map((i) => i.href).filter(
      (h) => !Object.values(TAB_HREFS).includes(h) && h !== "/settings",
    );
    expect([...hrefs(usedMost), ...hrefs(others)].sort()).toEqual(expected.sort());
  });

  it("Used most follows phoneRank, Others are alphabetical by the given label", () => {
    const { usedMost, others } = sheetPages();
    expect(hrefs(usedMost)).toEqual(["/runtimes", "/agents", "/insights", "/memory"]);
    const labels = others.map((i) => i.label);
    expect(labels).toEqual([...labels].sort((a, b) => a.localeCompare(b)));
    // German labels sort differently — the caller's label decides
    const de: Record<string, string> = { "/schedule": "Zeitplan", "/files": "Dateien" };
    const deOrder = hrefs(sheetPages((i) => de[i.href] ?? i.label).others);
    expect(deOrder.indexOf("/schedule")).toBe(deOrder.length - 1);
  });

  it("a switched-off vertical is not offered (same list as the sidebar)", () => {
    const withoutBench = NAV_ITEMS.filter((i) => i.href !== "/bench");
    expect(hrefs(sheetPages(undefined, withoutBench).others)).not.toContain("/bench");
  });

  it("a new page in nav.ts shows up without touching the sheet", () => {
    const extra: NavItem = { href: "/new-page", icon: Server, label: "Aardvark", labelKey: "x" };
    expect(hrefs(sheetPages(undefined, [...NAV_ITEMS, extra]).others)[0]).toBe("/new-page");
  });
});
