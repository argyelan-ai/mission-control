import { describe, it, expect, vi, beforeEach } from "vitest";
import { readFileSync } from "node:fs";
import path from "node:path";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

// P4 review SOLL-3: the open challenge lives in the URL (`/bench?challenge=<id>`).
// These tests pin the page half of that contract: URL → selection, selection →
// router.replace, tab clicks leave the detail, and the app route keeps the
// Suspense boundary that useSearchParams needs in the app router.

const nav = vi.hoisted(() => ({ search: "", replace: vi.fn() }));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: nav.replace, push: vi.fn() }),
  usePathname: () => "/bench",
  useSearchParams: () => new URLSearchParams(nav.search),
}));

vi.mock("@/components/layout/AppShell", () => ({
  default: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}));

vi.mock("../ChallengesTab", () => ({
  ChallengesTab: ({
    selectedId,
    onSelectedChange,
  }: {
    selectedId?: string | null;
    onSelectedChange?: (id: string | null) => void;
  }) => (
    <div>
      <span data-testid="selected">{selectedId ?? "none"}</span>
      <button onClick={() => onSelectedChange?.("ch-7")}>open-ch-7</button>
      <button onClick={() => onSelectedChange?.(null)}>close</button>
    </div>
  ),
}));

vi.mock("../PromptLibraryTab", () => ({
  PromptLibraryTab: () => <div data-testid="library" />,
}));

import BenchStudioPage from "../BenchStudioPage";

describe("BenchStudioPage — open challenge in the URL (P4, t2 SOLL-4)", () => {
  beforeEach(() => {
    nav.search = "";
    nav.replace.mockClear();
  });

  it("reads the open challenge from ?challenge=", () => {
    nav.search = "challenge=ch-9";
    render(<BenchStudioPage />);
    expect(screen.getByTestId("selected").textContent).toBe("ch-9");
  });

  it("no ?challenge= means the list", () => {
    render(<BenchStudioPage />);
    expect(screen.getByTestId("selected").textContent).toBe("none");
  });

  it("opening a challenge writes it into the URL and keeps other params", async () => {
    nav.search = "foo=1";
    render(<BenchStudioPage />);
    await userEvent.click(screen.getByRole("button", { name: "open-ch-7" }));
    expect(nav.replace).toHaveBeenCalledWith("/bench?foo=1&challenge=ch-7", { scroll: false });
  });

  it("closing the detail removes ?challenge= (bare path when nothing is left)", async () => {
    nav.search = "challenge=ch-9";
    render(<BenchStudioPage />);
    await userEvent.click(screen.getByRole("button", { name: "close" }));
    expect(nav.replace).toHaveBeenCalledWith("/bench", { scroll: false });
  });

  it("switching to the library leaves the open challenge", async () => {
    nav.search = "challenge=ch-9";
    render(<BenchStudioPage />);
    await userEvent.click(screen.getByRole("tab", { name: "Prompt Library" }));
    expect(nav.replace).toHaveBeenCalledWith("/bench", { scroll: false });
    expect(screen.getByTestId("library")).toBeTruthy();
  });

  it("a tap on the Challenges tab itself leads back to the list (also out of a dead link)", async () => {
    nav.search = "challenge=gone";
    render(<BenchStudioPage />);
    await userEvent.click(screen.getByRole("tab", { name: "Challenges" }));
    expect(nav.replace).toHaveBeenCalledWith("/bench", { scroll: false });
  });

  it("tab clicks without an open challenge do not touch the URL", async () => {
    render(<BenchStudioPage />);
    await userEvent.click(screen.getByRole("tab", { name: "Challenges" }));
    expect(nav.replace).not.toHaveBeenCalled();
  });
});

describe("bench app route — Suspense boundary for useSearchParams", () => {
  it("wraps BenchStudioPage in <Suspense> (Next app router needs it for useSearchParams)", () => {
    const src = readFileSync(path.resolve(__dirname, "../../../app/bench/page.tsx"), "utf8");
    expect(src).toMatch(/<Suspense[^>]*>\s*<BenchStudioPage\s*\/>\s*<\/Suspense>/);
  });
});
