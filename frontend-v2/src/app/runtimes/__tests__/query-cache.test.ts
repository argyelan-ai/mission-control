import { describe, it, expect, beforeEach, vi } from "vitest";
import { loadCached, saveCached, fetchWithCache } from "../queryCache";

// Other suites in the shared worker stub `localStorage` with partial mocks
// (getItem/setItem only) and never unstub — install a complete in-memory
// implementation so this suite is independent of test scheduling.
function freshStorage() {
  const store = new Map<string, string>();
  return {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => void store.set(k, String(v)),
    removeItem: (k: string) => void store.delete(k),
    clear: () => store.clear(),
    key: (i: number) => [...store.keys()][i] ?? null,
    get length() {
      return store.size;
    },
  };
}

describe("queryCache", () => {
  beforeEach(() => {
    vi.stubGlobal("localStorage", freshStorage());
  });

  it("round-trips a payload", () => {
    saveCached("runtimes", { runtimes: [{ id: "a" }] });
    expect(loadCached("runtimes")).toEqual({ runtimes: [{ id: "a" }] });
  });

  it("returns undefined for missing, corrupt, or wrong-version entries", () => {
    expect(loadCached("nope")).toBeUndefined();
    localStorage.setItem("mc-runtimes-cache:bad", "{not json");
    expect(loadCached("bad")).toBeUndefined();
    localStorage.setItem("mc-runtimes-cache:old", JSON.stringify({ v: 0, at: Date.now(), data: 1 }));
    expect(loadCached("old")).toBeUndefined();
  });

  it("expires entries older than the max age", () => {
    localStorage.setItem(
      "mc-runtimes-cache:stale",
      JSON.stringify({ v: 1, at: Date.now() - 25 * 60 * 60 * 1000, data: 1 })
    );
    expect(loadCached("stale")).toBeUndefined();
  });

  it("fetchWithCache persists the fresh result", async () => {
    const result = await fetchWithCache("k", async () => ({ fresh: true }));
    expect(result).toEqual({ fresh: true });
    expect(loadCached("k")).toEqual({ fresh: true });
  });
});
