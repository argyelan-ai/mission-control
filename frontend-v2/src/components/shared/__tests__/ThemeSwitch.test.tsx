import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ThemeCycleButton, ThemeSegmented } from "../ThemeSwitch";
import { THEME_STORAGE_KEY, setTheme } from "@/lib/theme";

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

beforeEach(() => {
  vi.stubGlobal("localStorage", memoryStorage());
  vi.stubGlobal("matchMedia", vi.fn(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })));
  act(() => setTheme("dark"));
});
afterEach(() => vi.unstubAllGlobals());

describe("ThemeSegmented", () => {
  it("size touch gives every option a 44px target (phone menu, WCAG 2.5.5)", () => {
    render(<ThemeSegmented size="touch" />);
    for (const r of screen.getAllByRole("radio")) {
      expect((r as HTMLElement).style.height).toBe("44px");
    }
  });

  it("offers Dark (default) · Light · System as a radio group, Dark checked", () => {
    render(<ThemeSegmented />);
    const group = screen.getByRole("radiogroup", { name: "Theme" });
    const radios = Array.from(group.querySelectorAll('[role="radio"]'));
    expect(radios.map((r) => r.textContent)).toEqual(["Dark", "Light", "System"]);
    expect(screen.getByRole("radio", { name: "Dark" })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByRole("radio", { name: "Light" })).toHaveAttribute("aria-checked", "false");
  });

  it("clicking Light switches the page and remembers it in this browser", () => {
    render(<ThemeSegmented />);
    fireEvent.click(screen.getByRole("radio", { name: "Light" }));
    expect(document.documentElement.getAttribute("data-theme")).toBe("light");
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe("light");
    expect(screen.getByRole("radio", { name: "Light" })).toHaveAttribute("aria-checked", "true");
  });

  it("a second switch on the same page follows (settings + user menu stay in sync)", () => {
    render(<><div data-testid="a"><ThemeSegmented /></div><div data-testid="b"><ThemeCycleButton /></div></>);
    fireEvent.click(screen.getByRole("radio", { name: "System" }));
    expect(screen.getByTestId("theme-cycle")).toHaveAttribute("data-choice", "system");
  });

  it("arrow keys move the choice (radio-group keyboard pattern)", () => {
    render(<ThemeSegmented />);
    fireEvent.keyDown(screen.getByRole("radio", { name: "Dark" }), { key: "ArrowRight" });
    expect(document.documentElement.getAttribute("data-theme")).toBe("light");
    expect(screen.getByRole("radio", { name: "Light" })).toHaveFocus();
  });
});

describe("ThemeCycleButton", () => {
  it("names the current theme and the next one, and cycles Dark → Light → System → Dark", () => {
    render(<ThemeCycleButton />);
    const btn = screen.getByTestId("theme-cycle");
    expect(btn).toHaveAttribute("aria-label", "Theme: Dark — switch to Light");
    fireEvent.click(btn);
    expect(document.documentElement.getAttribute("data-theme")).toBe("light");
    expect(btn).toHaveAttribute("aria-label", "Theme: Light — switch to System");
    fireEvent.click(btn);
    expect(btn).toHaveAttribute("data-choice", "system");
    fireEvent.click(btn);
    expect(btn).toHaveAttribute("data-choice", "dark");
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe("dark");
  });
});
