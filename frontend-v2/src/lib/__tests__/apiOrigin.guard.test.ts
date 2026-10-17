/**
 * Guard: the browser talks to the API same-origin (relative `/api/...`,
 * routed by Caddy), never to a baked-in `localhost:8000`.
 *
 * Finding 17.10.2026 (fresh install, opened from another device): the stream
 * URLs pointed at http://localhost:8000 — on the viewer's laptop or phone
 * that is the viewer's own machine, so every live update failed. The cause
 * was the published frontend image: it was built before #603 removed the
 * `?? "http://localhost:8000"` fallbacks, and `NEXT_PUBLIC_*` values are
 * frozen into the bundle at build time, so no runtime setting could fix it.
 *
 * Two checks:
 *  1. one place reads NEXT_PUBLIC_API_URL (lib/authToken.ts) and its fallback
 *     is "" — a second reader is how the old fallbacks crept in;
 *  2. no app source carries a localhost:8000 URL at all.
 * The built bundle itself is checked by scripts/check-bundle-origins.mjs
 * (CI, after the image build).
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative, sep } from "node:path";
import { describe, it, expect, afterEach, vi } from "vitest";

const SRC = join(__dirname, "..", "..");

function sourceFiles(dir: string): string[] {
  const out: string[] = [];
  for (const name of readdirSync(dir)) {
    if (name === "__tests__" || name === "__fixtures__" || name === "node_modules") continue;
    const p = join(dir, name);
    if (statSync(p).isDirectory()) out.push(...sourceFiles(p));
    else if (/\.(ts|tsx)$/.test(name) && !/\.(test|spec)\.tsx?$/.test(name)) out.push(p);
  }
  return out;
}

const FILES = sourceFiles(SRC).map((p) => ({
  rel: relative(SRC, p).split(sep).join("/"),
  text: readFileSync(p, "utf8"),
}));

describe("API origin guard", () => {
  it("reads NEXT_PUBLIC_API_URL in exactly one place", () => {
    const readers = FILES.filter((f) => /process\.env\.NEXT_PUBLIC_API_URL/.test(f.text)).map(
      (f) => f.rel,
    );
    expect(readers).toEqual(["lib/authToken.ts"]);
  });

  it("falls back to same-origin, not to a host", () => {
    const authToken = FILES.find((f) => f.rel === "lib/authToken.ts")!.text;
    expect(authToken).toMatch(/process\.env\.NEXT_PUBLIC_API_URL \?\? ""/);
  });

  it("no app source carries a localhost:8000 URL", () => {
    const hits = FILES.filter((f) => /(localhost|127\.0\.0\.1):8000/.test(f.text)).map(
      (f) => f.rel,
    );
    expect(hits).toEqual([]);
  });
});

describe("API base without NEXT_PUBLIC_API_URL (the prebuilt-image case)", () => {
  const original = process.env.NEXT_PUBLIC_API_URL;
  afterEach(() => {
    vi.unstubAllGlobals();
    if (original === undefined) delete process.env.NEXT_PUBLIC_API_URL;
    else process.env.NEXT_PUBLIC_API_URL = original;
  });

  it("builds relative request, stream and socket URLs", async () => {
    vi.resetModules();
    delete process.env.NEXT_PUBLIC_API_URL;
    const fetchMock = vi.fn(
      async () => new Response("{}", { status: 200, headers: { "Content-Type": "application/json" } }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const api = await import("@/lib/api");

    expect(api.BASE_URL).toBe("");
    await api.request("/api/v1/boards");
    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/boards");
    for (const build of Object.values(api.sseUrls)) {
      const url = (build as (...a: string[]) => string)("x");
      expect(url.startsWith("/api/")).toBe(true);
    }
  });
});
