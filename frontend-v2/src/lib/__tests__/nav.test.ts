import { describe, it, expect } from "vitest";
import {
  NAV_ITEMS,
  NAV_TREE,
  CHROME_ITEMS,
  DEFAULT_PINS,
  resolveNav,
  isActiveRoute,
  groupKeyFor,
  navItem,
} from "../nav";

describe("nav model", () => {
  it("exposes every destination exactly once in the flat list", () => {
    const hrefs = NAV_ITEMS.map((i) => i.href);
    expect(new Set(hrefs).size).toBe(hrefs.length);
  });

  it("puts every flat destination into exactly one group, or marks it chrome-level", () => {
    for (const item of NAV_ITEMS) {
      const groups = NAV_TREE.filter((g) =>
        g.children.some((c) => c.href === item.href)
      );
      const expected = CHROME_ITEMS.includes(item.href) ? 0 : 1;
      expect(groups, `${item.href} must appear in exactly ${expected} group(s)`).toHaveLength(expected);
    }
  });

  it("keeps chrome-level destinations out of every group so they never show twice", () => {
    const inGroups = NAV_TREE.flatMap((g) => g.children.map((c) => c.href));
    for (const href of CHROME_ITEMS) expect(inGroups).not.toContain(href);
  });

  it("still lists chrome-level destinations in the flat model, so search finds them", () => {
    for (const href of CHROME_ITEMS) expect(navItem(href)).toBeDefined();
  });

  it("has no group child that is missing from the flat list", () => {
    const known = new Set(NAV_ITEMS.map((i) => i.href));
    for (const g of NAV_TREE) {
      for (const c of g.children) expect(known.has(c.href)).toBe(true);
    }
  });
});

describe("resolveNav", () => {
  it("returns the pinned items in the user's order", () => {
    const { pinned } = resolveNav(["/inbox", "/"]);
    expect(pinned.map((i) => i.href)).toEqual(["/inbox", "/"]);
  });

  it("never shows a pinned destination a second time inside a group", () => {
    const { pinned, groups } = resolveNav(DEFAULT_PINS);
    const pinnedHrefs = new Set(pinned.map((i) => i.href));
    const inGroups = groups.flatMap((g) => g.children.map((c) => c.href));
    for (const href of inGroups) expect(pinnedHrefs.has(href)).toBe(false);
  });

  it("keeps every destination reachable — pinned plus grouped covers all", () => {
    const { pinned, groups } = resolveNav(DEFAULT_PINS);
    const reachable = new Set([
      ...pinned.map((i) => i.href),
      ...groups.flatMap((g) => g.children.map((c) => c.href)),
      ...CHROME_ITEMS, // reachable from the user area, not from a group
    ]);
    for (const item of NAV_ITEMS) {
      expect(reachable.has(item.href), `${item.href} unreachable`).toBe(true);
    }
  });

  it("drops groups that were emptied by pinning", () => {
    const studio = NAV_TREE.find((g) => g.key === "studio");
    const allStudio = studio!.children.map((c) => c.href);
    const { groups } = resolveNav(allStudio);
    expect(groups.some((g) => g.key === "studio")).toBe(false);
  });

  it("ignores unknown routes in the pin list", () => {
    const { pinned } = resolveNav(["/", "/does-not-exist"]);
    expect(pinned.map((i) => i.href)).toEqual(["/"]);
  });

  it("treats a missing pin list as nothing pinned rather than crashing", () => {
    // State persisted by an older build has no pinnedNav at all.
    const { pinned, groups } = resolveNav(undefined);
    expect(pinned).toHaveLength(0);
    expect(groups.flatMap((g) => g.children)).toHaveLength(
      NAV_ITEMS.length - CHROME_ITEMS.length
    );
  });

  it("shows the full tree when nothing is pinned", () => {
    const { pinned, groups } = resolveNav([]);
    expect(pinned).toHaveLength(0);
    expect(groups.flatMap((g) => g.children)).toHaveLength(
      NAV_ITEMS.length - CHROME_ITEMS.length
    );
  });
});

describe("isActiveRoute", () => {
  it("matches root only exactly", () => {
    expect(isActiveRoute("/", "/")).toBe(true);
    expect(isActiveRoute("/", "/tasks")).toBe(false);
  });

  it("matches sub-routes by prefix", () => {
    expect(isActiveRoute("/agents", "/agents/abc-123")).toBe(true);
    expect(isActiveRoute("/agents", "/tasks")).toBe(false);
  });
});

describe("groupKeyFor", () => {
  it("finds the group a route belongs to", () => {
    expect(groupKeyFor("/runtimes")).toBe("system");
    expect(groupKeyFor("/files")).toBe("knowledge");
  });

  it("returns undefined for an unknown route", () => {
    expect(groupKeyFor("/nope")).toBeUndefined();
  });
});

describe("defaults", () => {
  it("pins only routes that exist", () => {
    for (const href of DEFAULT_PINS) expect(navItem(href)).toBeDefined();
  });
});

describe("i18n", () => {
  it("gives every destination a translation key", () => {
    for (const item of NAV_ITEMS) expect(item.labelKey).toBeTruthy();
  });

  it("gives every group both a heading and a row translation key", () => {
    for (const g of NAV_TREE) {
      expect(g.labelKey).toBeTruthy();
      expect(g.rowLabelKey).toBeTruthy();
    }
  });
});
