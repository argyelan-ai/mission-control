import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { AppearanceSection } from "../AppearanceSection";
import { THEME_STORAGE_KEY, setTheme } from "@/lib/theme";

beforeEach(() => {
  const m = new Map<string, string>();
  vi.stubGlobal("localStorage", {
    getItem: (k: string) => m.get(k) ?? null,
    setItem: (k: string, v: string) => void m.set(k, v),
    removeItem: (k: string) => void m.delete(k),
  });
  vi.stubGlobal("matchMedia", vi.fn(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })));
  act(() => setTheme("dark"));
});

describe("Settings → Appearance", () => {
  it("shows the switch with Dark marked as the default and explains where it is stored", () => {
    render(<AppearanceSection />);
    expect(screen.getByRole("heading", { name: "Appearance" })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "Dark" })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByText(/Dark is the default/)).toBeInTheDocument();
    expect(screen.getByText(/saved in this browser/i)).toBeInTheDocument();
  });

  it("switching to Light applies it at once and saves it", () => {
    render(<AppearanceSection />);
    fireEvent.click(screen.getByRole("radio", { name: "Light" }));
    expect(document.documentElement.getAttribute("data-theme")).toBe("light");
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe("light");
  });
});
