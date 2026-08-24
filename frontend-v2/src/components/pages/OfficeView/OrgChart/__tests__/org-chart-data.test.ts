/**
 * The org chart is the most product-visible place the author's own fleet ever
 * reached: a complete roster with names, roles and taglines, served live at
 * `/office` to everyone who installs Mission Control.
 *
 * It is example data — a crew of ROLES, so a new operator sees what the shape
 * is meant to look like. These tests keep it that way, and keep the two ids
 * the layout code reaches for (`jarvis`, `boss`) intact while doing it.
 */
import { describe, expect, it } from "vitest";

import { ORG_CHART, getChildren, getRoot } from "../org-chart-data";

// Agent names from the author's private fleet. Same list as
// scripts/privacy-scan.py; if one shows up here it is live on someone's
// screen, not just in a comment.
const FLEET_NAMES = ["sparky", "shakespeare", "freecode", "davinci"];

describe("org chart example data", () => {
  it("names no agent from anyone's private fleet", () => {
    const blob = JSON.stringify(
      ORG_CHART.nodes.map(({ id, name, roleKey, taglineKey }) => ({
        id,
        name,
        roleKey,
        taglineKey,
      })),
    ).toLowerCase();

    for (const name of FLEET_NAMES) {
      expect(blob).not.toContain(name);
    }
  });

  it("names no person at the root", () => {
    const root = getRoot();
    expect(root).not.toBeNull();
    // The root is whoever installed this — a role, never a name.
    expect(root!.id).toBe("operator");
    expect(root!.runtime).toBe("human");
  });

  it("keeps the two ids the layout reaches for", () => {
    // OrgChart/index.tsx looks these up by id to draw the voice branch and to
    // hang the workers off the lead; renaming them silently empties the chart.
    const ids = ORG_CHART.nodes.map((n) => n.id);
    expect(ids).toContain("jarvis");
    expect(ids).toContain("boss");
  });

  it("still describes a complete crew under the lead", () => {
    const workers = getChildren("boss");
    expect(workers.length).toBeGreaterThanOrEqual(6);
    expect(new Set(workers.map((w) => w.id)).size).toBe(workers.length);
    for (const w of workers) {
      expect(w.tier).toBe("worker");
      expect(w.taglineKey).toMatch(/^tagline[A-Z]/);
    }
  });

  it("has exactly one root and no orphans", () => {
    const ids = new Set(ORG_CHART.nodes.map((n) => n.id));
    const roots = ORG_CHART.nodes.filter((n) => n.parentId === null);
    expect(roots).toHaveLength(1);
    for (const n of ORG_CHART.nodes) {
      if (n.parentId !== null) expect(ids.has(n.parentId)).toBe(true);
    }
  });
});
