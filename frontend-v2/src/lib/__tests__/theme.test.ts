import { beforeEach, describe, expect, it, vi } from "vitest";
import { THEME_STORAGE_KEY, applyTheme, readStoredTheme, resolveTheme, setTheme, THEME_INIT_SCRIPT } from "../theme";

// The test runtime exposes a partial `localStorage` global (no clear()); use an
// in-memory stand-in so the tests exercise our code, not the environment.
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

describe("theme", () => {
  beforeEach(() => {
    vi.stubGlobal("localStorage", memoryStorage());
    document.documentElement.removeAttribute("data-theme");
    document.documentElement.style.colorScheme = "";
  });

  it("defaults to dark when nothing is stored", () => {
    expect(readStoredTheme()).toBe("dark");
    expect(resolveTheme("dark", false)).toBe("dark");
  });

  it("resolves 'system' from the OS preference", () => {
    expect(resolveTheme("system", true)).toBe("light");
    expect(resolveTheme("system", false)).toBe("dark");
  });

  it("applyTheme stamps data-theme and color-scheme on <html>", () => {
    applyTheme("light");
    expect(document.documentElement.getAttribute("data-theme")).toBe("light");
    expect(document.documentElement.style.colorScheme).toBe("light");
    applyTheme("dark");
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
    expect(document.documentElement.style.colorScheme).toBe("dark");
  });

  it("setTheme persists the choice and applies it", () => {
    setTheme("light");
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe("light");
    expect(document.documentElement.getAttribute("data-theme")).toBe("light");
  });

  it("ignores garbage in storage", () => {
    localStorage.setItem(THEME_STORAGE_KEY, "neon");
    expect(readStoredTheme()).toBe("dark");
  });

  it("ships a pre-paint init script that reads the same key", () => {
    expect(THEME_INIT_SCRIPT).toContain(THEME_STORAGE_KEY);
    expect(THEME_INIT_SCRIPT).toContain("data-theme");
    // Must be a self-contained expression — no imports, no top-level await.
    expect(THEME_INIT_SCRIPT).not.toMatch(/import\s|require\(/);
  });
});
