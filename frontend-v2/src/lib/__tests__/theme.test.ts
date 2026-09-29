import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  THEME_COLOR,
  THEME_INIT_SCRIPT,
  THEME_STORAGE_KEY,
  applyTheme,
  readStoredTheme,
  resolveTheme,
  setTheme,
  startThemeSync,
  useTheme,
} from "../theme";

// The test runtime exposes a partial `localStorage` global; use an in-memory
// stand-in so the tests exercise our code, not the environment.
function memoryStorage(): Storage {
  const m = new Map<string, string>();
  return {
    get length() { return m.size; },
    clear: () => m.clear(),
    getItem: (k: string) => (m.has(k) ? m.get(k)! : null),
    key: (i: number) => Array.from(m.keys())[i] ?? null,
    removeItem: (k: string) => { m.delete(k); },
    setItem: (k: string, v: string) => { m.set(k, String(v)); },
  } as Storage;
}

function throwingStorage(): Storage {
  const boom = () => { throw new Error("SecurityError: storage disabled"); };
  return { length: 0, clear: boom, getItem: boom, key: boom, removeItem: boom, setItem: boom } as unknown as Storage;
}

// Controllable prefers-color-scheme media query.
function fakeMatchMedia(initialLight: boolean) {
  const listeners = new Set<(e: { matches: boolean }) => void>();
  const mql = {
    matches: initialLight,
    media: "(prefers-color-scheme: light)",
    addEventListener: (_: string, fn: (e: { matches: boolean }) => void) => listeners.add(fn),
    removeEventListener: (_: string, fn: (e: { matches: boolean }) => void) => listeners.delete(fn),
  };
  vi.stubGlobal("matchMedia", vi.fn(() => mql));
  return {
    set(light: boolean) {
      mql.matches = light;
      listeners.forEach((fn) => fn({ matches: light }));
    },
    listenerCount: () => listeners.size,
  };
}

const html = () => document.documentElement;
const metaColor = () => document.querySelector('meta[name="theme-color"]')?.getAttribute("content");

describe("theme", () => {
  let stop: (() => void) | undefined;

  beforeEach(() => {
    vi.stubGlobal("localStorage", memoryStorage());
    fakeMatchMedia(false);
    html().removeAttribute("data-theme");
    html().style.colorScheme = "";
    document.head.innerHTML = '<meta name="theme-color" content="#0A0A0A">';
  });

  afterEach(() => {
    stop?.();
    stop = undefined;
    vi.unstubAllGlobals();
  });

  it("defaults to dark when nothing is stored", () => {
    expect(readStoredTheme()).toBe("dark");
  });

  it("ignores garbage in storage", () => {
    localStorage.setItem(THEME_STORAGE_KEY, "neon");
    expect(readStoredTheme()).toBe("dark");
  });

  it("falls back to dark without crashing when storage throws (private mode)", () => {
    vi.stubGlobal("localStorage", throwingStorage());
    expect(readStoredTheme()).toBe("dark");
    expect(() => setTheme("light")).not.toThrow();
    // still applied for this page even though it could not be saved
    expect(html().getAttribute("data-theme")).toBe("light");
  });

  it("resolves 'system' from the OS preference", () => {
    expect(resolveTheme("system", true)).toBe("light");
    expect(resolveTheme("system", false)).toBe("dark");
    expect(resolveTheme("dark", true)).toBe("dark");
    expect(resolveTheme("light", false)).toBe("light");
  });

  it("applyTheme stamps data-theme, color-scheme and the browser theme-color", () => {
    applyTheme("light");
    expect(html().getAttribute("data-theme")).toBe("light");
    expect(html().style.colorScheme).toBe("light");
    expect(metaColor()).toBe(THEME_COLOR.light);
    applyTheme("dark");
    expect(html().getAttribute("data-theme")).toBe("dark");
    expect(html().style.colorScheme).toBe("dark");
    expect(metaColor()).toBe(THEME_COLOR.dark);
  });

  it("setTheme persists the choice and applies it", () => {
    setTheme("light");
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe("light");
    expect(html().getAttribute("data-theme")).toBe("light");
  });

  it("'system' follows a live OS switch — and 'dark' does not", () => {
    const mm = fakeMatchMedia(false);
    stop = startThemeSync();
    setTheme("system");
    expect(html().getAttribute("data-theme")).toBe("dark");
    mm.set(true);
    expect(html().getAttribute("data-theme")).toBe("light");
    setTheme("dark");
    mm.set(true);
    expect(html().getAttribute("data-theme")).toBe("dark");
  });

  it("follows a choice made in another tab (storage event)", () => {
    stop = startThemeSync();
    expect(html().getAttribute("data-theme")).toBe("dark");
    localStorage.setItem(THEME_STORAGE_KEY, "light");
    window.dispatchEvent(new StorageEvent("storage", { key: THEME_STORAGE_KEY, newValue: "light" }));
    expect(html().getAttribute("data-theme")).toBe("light");
  });

  it("startThemeSync cleans up its listeners", () => {
    const mm = fakeMatchMedia(false);
    const s = startThemeSync();
    expect(mm.listenerCount()).toBe(1);
    s();
    expect(mm.listenerCount()).toBe(0);
  });

  it("useTheme re-renders every consumer on a switch", () => {
    stop = startThemeSync();
    const a = renderHook(() => useTheme());
    const b = renderHook(() => useTheme());
    expect(a.result.current.choice).toBe("dark");
    act(() => a.result.current.setTheme("light"));
    expect(a.result.current).toMatchObject({ choice: "light", resolved: "light" });
    expect(b.result.current).toMatchObject({ choice: "light", resolved: "light" });
  });

  describe("pre-paint init script (runs in <head> before first paint)", () => {
    const run = () => new Function(THEME_INIT_SCRIPT)();

    it("applies a stored light choice immediately", () => {
      localStorage.setItem(THEME_STORAGE_KEY, "light");
      run();
      expect(html().getAttribute("data-theme")).toBe("light");
      expect(html().style.colorScheme).toBe("light");
    });

    it("applies dark when nothing is stored", () => {
      run();
      expect(html().getAttribute("data-theme")).toBe("dark");
    });

    it("resolves 'system' via prefers-color-scheme", () => {
      fakeMatchMedia(true);
      localStorage.setItem(THEME_STORAGE_KEY, "system");
      run();
      expect(html().getAttribute("data-theme")).toBe("light");
    });

    it("never throws, even when storage is blocked", () => {
      vi.stubGlobal("localStorage", throwingStorage());
      expect(run).not.toThrow();
    });

    it("is self-contained (no imports)", () => {
      expect(THEME_INIT_SCRIPT).not.toMatch(/import\s|require\(/);
    });
  });
});
