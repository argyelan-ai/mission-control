import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterAll, describe, expect, it } from "vitest";
import { discoverRoutes, filterRoutes, firstId, pageFileToRoute, resolveRoutes } from "../lib/routes.mjs";

const dir = mkdtempSync(join(tmpdir(), "probe-routes-"));
const touch = (p) => {
  mkdirSync(join(dir, p, ".."), { recursive: true });
  writeFileSync(join(dir, p), "export default function P() { return null }");
};
["page.tsx", "tasks/page.tsx", "agents/page.tsx", "agents/[id]/page.tsx", "schedule/[jobId]/page.tsx", "(group)/inbox/page.tsx", "_private/x/page.tsx", "files/[...slug]/page.tsx", "tasks/__tests__/page.tsx", "tasks/Board.tsx"].forEach(touch);
afterAll(() => rmSync(dir, { recursive: true, force: true }));

describe("pageFileToRoute", () => {
  it("maps app files to URL patterns", () => {
    expect(pageFileToRoute("page.tsx")).toBe("/");
    expect(pageFileToRoute("agents/[id]/page.tsx")).toBe("/agents/[id]");
    expect(pageFileToRoute("(marketing)/about/page.tsx")).toBe("/about");
    expect(pageFileToRoute("@modal/login/page.tsx")).toBe("/login");
    expect(pageFileToRoute("_lib/page.tsx")).toBeNull();
  });
});

describe("discoverRoutes", () => {
  it("finds every page, skips private folders, tests and non-page files", () => {
    const pats = discoverRoutes(dir).map((r) => r.pattern);
    expect(pats.sort()).toEqual(["/", "/agents", "/agents/[id]", "/files/[...slug]", "/inbox", "/schedule/[jobId]", "/tasks"]);
    expect(discoverRoutes(dir).find((r) => r.pattern === "/agents/[id]").dynamic).toBe(true);
  });
});

describe("filterRoutes", () => {
  const routes = discoverRoutes(dir);
  it("matches exact patterns and /* prefixes", () => {
    expect(filterRoutes(routes, ["/tasks"]).map((r) => r.pattern)).toEqual(["/tasks"]);
    expect(filterRoutes(routes, ["/agents/*"]).map((r) => r.pattern).sort()).toEqual(["/agents", "/agents/[id]"]);
    expect(filterRoutes(routes, []).length).toBe(routes.length);
  });
});

describe("firstId", () => {
  it("reads arrays and common envelopes", () => {
    expect(firstId([{ id: "a" }, { id: "b" }])).toBe("a");
    expect(firstId({ items: [{ id: 7 }] })).toBe("7");
    expect(firstId({ data: [{ nope: 1 }] })).toBeNull();
    expect(firstId(null)).toBeNull();
  });
});

describe("resolveRoutes", () => {
  it("fills dynamic ids from a GET source and reports what it cannot resolve", async () => {
    const calls = [];
    const getJson = async (ep) => {
      calls.push(ep);
      if (ep === "/api/v1/agents") return [{ id: "abc" }];
      if (ep === "/api/v1/schedule/jobs") return [];
      throw new Error("unexpected");
    };
    const { resolved, skipped } = await resolveRoutes(discoverRoutes(dir), getJson);
    expect(resolved.find((r) => r.pattern === "/agents/[id]").path).toBe("/agents/abc");
    expect(resolved.find((r) => r.pattern === "/tasks").path).toBe("/tasks");
    expect(skipped.map((s) => s.pattern).sort()).toEqual(["/files/[...slug]", "/schedule/[jobId]"]);
    expect(skipped.find((s) => s.pattern === "/schedule/[jobId]").reason).toMatch(/no items/);
    expect(calls.sort()).toEqual(["/api/v1/agents", "/api/v1/schedule/jobs"]);
  });

  it("reports a failing source instead of throwing", async () => {
    const { skipped } = await resolveRoutes([{ pattern: "/agents/[id]", dynamic: true }], async () => {
      throw new Error("HTTP 401");
    });
    expect(skipped[0].reason).toMatch(/HTTP 401/);
  });
});
