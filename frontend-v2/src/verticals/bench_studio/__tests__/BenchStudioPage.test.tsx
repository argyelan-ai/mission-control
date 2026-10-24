import { describe, it, expect, vi, beforeEach } from "vitest";
import { readFileSync } from "node:fs";
import path from "node:path";
import { render, screen } from "@testing-library/react";

// Bench W1 (package D): /bench is the test list (TestList, P7); the prompt
// library lives under ?view=library; the test sheet (TestSheetHost) is always
// mounted; an old `/bench?challenge=<id>` link lands on the result page
// `/bench/r/<id>` (the challenge id IS the test id).

const nav = vi.hoisted(() => ({ search: "", replace: vi.fn() }));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: nav.replace, push: vi.fn() }),
  usePathname: () => "/bench",
  useSearchParams: () => new URLSearchParams(nav.search),
}));

vi.mock("@/components/layout/AppShell", () => ({
  default: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}));

vi.mock("../w1/list/TestList", () => ({
  TestList: () => <div data-testid="test-list" />,
}));

vi.mock("../w1/sheet/TestSheetHost", () => ({
  TestSheetHost: () => <div data-testid="test-sheet-host" />,
}));

vi.mock("../PromptLibraryTab", () => ({
  PromptLibraryTab: () => <div data-testid="library" />,
}));

import BenchStudioPage from "../BenchStudioPage";

describe("BenchStudioPage — W1 list, library, sheet", () => {
  beforeEach(() => {
    nav.search = "";
    nav.replace.mockClear();
  });

  it("shows the test list and mounts the test sheet", () => {
    render(<BenchStudioPage />);
    expect(screen.getByTestId("test-list")).toBeInTheDocument();
    expect(screen.getByTestId("test-sheet-host")).toBeInTheDocument();
    expect(screen.queryByTestId("library")).toBeNull();
    expect(nav.replace).not.toHaveBeenCalled();
  });

  it("the sheet stays mounted while ?test=1 is set", () => {
    nav.search = "test=1&from=quick";
    render(<BenchStudioPage />);
    expect(screen.getByTestId("test-list")).toBeInTheDocument();
    expect(screen.getByTestId("test-sheet-host")).toBeInTheDocument();
  });

  it("?view=library shows the prompt library with a way back", () => {
    nav.search = "view=library";
    render(<BenchStudioPage />);
    expect(screen.getByTestId("library")).toBeInTheDocument();
    expect(screen.queryByTestId("test-list")).toBeNull();
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Prompt library");
    expect(screen.getByRole("link", { name: /Benchmark/ })).toHaveAttribute("href", "/bench");
    expect(screen.getByTestId("test-sheet-host")).toBeInTheDocument();
  });

  it("an old ?challenge=<id> link goes to the result page", () => {
    nav.search = "challenge=ch-9";
    render(<BenchStudioPage />);
    expect(nav.replace).toHaveBeenCalledWith("/bench/r/ch-9");
    expect(screen.queryByTestId("test-list")).toBeNull();
  });
});

describe("bench app route — Suspense boundary for useSearchParams", () => {
  it("wraps BenchStudioPage in <Suspense> (Next app router needs it for useSearchParams)", () => {
    const src = readFileSync(path.resolve(__dirname, "../../../app/bench/page.tsx"), "utf8");
    expect(src).toMatch(/<Suspense[^>]*>\s*<BenchStudioPage\s*\/>\s*<\/Suspense>/);
  });
});

describe("legacy UI is gone (W1.8 kill list)", () => {
  it("the old dialog, detail and list files no longer exist", () => {
    const dir = path.resolve(__dirname, "..");
    for (const f of [
      "NewChallengeDialog.tsx",
      "EffortChips.tsx",
      "ChallengeDetail.tsx",
      "DraftDialog.tsx",
      "EntryEffort.tsx",
      "EntryTaskState.tsx",
      "ChallengesTab.tsx",
    ]) {
      expect(() => readFileSync(path.join(dir, f)), f).toThrow();
    }
  });

  it("the legacy client no longer offers the removed routes", () => {
    const src = readFileSync(path.resolve(__dirname, "../api.ts"), "utf8");
    expect(src).not.toMatch(/spark-models|\/draft|effort-levels/);
  });
});
