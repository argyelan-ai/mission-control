import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { PromptTemplate } from "../types";

vi.mock("@/verticals/bench_studio/api", () => ({
  benchApi: {
    challenges: {
      list: vi.fn().mockResolvedValue([]),
      get: vi.fn(), create: vi.fn(), draft: vi.fn(), rerender: vi.fn(),
    },
    entries: { retry: vi.fn() },
    promptTemplates: {
      list: vi.fn(),
      create: vi.fn(),
      update: vi.fn(),
      remove: vi.fn().mockResolvedValue(undefined),
    },
    sharedSubpath: (p: string) => p,
  },
}));

import { benchApi } from "@/verticals/bench_studio/api";
import { PromptLibraryTab } from "../PromptLibraryTab";

const TPL: PromptTemplate = {
  id: "tpl-1",
  title: "Bouncing balls",
  body: "100 bouncing balls in one index.html",
  tags: ["animation", "physics"],
  created_at: "2026-07-11T09:00:00Z",
  updated_at: "2026-07-11T09:00:00Z",
};

function renderTab(onStart = vi.fn()) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={qc}>
      <PromptLibraryTab onStartChallenge={onStart} />
    </QueryClientProvider>
  );
  return onStart;
}

describe("PromptLibraryTab", () => {
  beforeEach(() => {
    vi.mocked(benchApi.promptTemplates.list).mockResolvedValue([TPL]);
    vi.mocked(benchApi.promptTemplates.create).mockResolvedValue({
      ...TPL, id: "tpl-2", title: "New",
    });
    vi.mocked(benchApi.promptTemplates.update).mockResolvedValue(TPL);
  });

  it("lists templates with tags", async () => {
    renderTab();
    expect(await screen.findByText("Bouncing balls")).toBeTruthy();
    expect(screen.getByText("animation")).toBeTruthy();
  });

  it("filters by search", async () => {
    renderTab();
    await screen.findByText("Bouncing balls");
    await userEvent.type(screen.getByPlaceholderText(/Suche/), "zzz");
    expect(screen.queryByText("Bouncing balls")).toBeNull();
  });

  it("creates a template via the editor", async () => {
    renderTab();
    await userEvent.click(await screen.findByRole("button", { name: /Neues Template/ }));
    await userEvent.type(screen.getByPlaceholderText("Titel"), "New");
    await userEvent.type(screen.getByPlaceholderText(/Prompt-Text/), "body text");
    await userEvent.type(screen.getByPlaceholderText(/Tags/), "a, b");
    await userEvent.click(screen.getByRole("button", { name: /Speichern/ }));
    await waitFor(() =>
      expect(benchApi.promptTemplates.create).toHaveBeenCalledWith({
        title: "New",
        body: "body text",
        tags: ["a", "b"],
      })
    );
  });

  it("deletes a template with confirmation", async () => {
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    renderTab();
    await screen.findByText("Bouncing balls");
    await userEvent.click(screen.getByRole("button", { name: /Löschen/ }));
    await waitFor(() =>
      expect(benchApi.promptTemplates.remove).toHaveBeenCalledWith("tpl-1")
    );
    expect(confirmSpy).toHaveBeenCalledWith(`Template "Bouncing balls" wirklich löschen?`);
    confirmSpy.mockRestore();
  });

  it("fires onStartChallenge with the template", async () => {
    const onStart = renderTab();
    await screen.findByText("Bouncing balls");
    await userEvent.click(screen.getByRole("button", { name: /Challenge starten/ }));
    expect(onStart).toHaveBeenCalledWith(TPL);
  });

  it("filters templates by tag", async () => {
    const TPL2: PromptTemplate = {
      id: "tpl-2",
      title: "Color picker",
      body: "HTML color picker component",
      tags: ["ui-component"],
      created_at: "2026-07-11T10:00:00Z",
      updated_at: "2026-07-11T10:00:00Z",
    };
    vi.mocked(benchApi.promptTemplates.list).mockResolvedValue([TPL, TPL2]);
    renderTab();
    await screen.findByText("Bouncing balls");
    expect(screen.getByText("Color picker")).toBeTruthy();

    // Click "animation" tag filter
    await userEvent.click(screen.getByRole("button", { name: "animation" }));

    // Only TPL with "animation" tag remains
    expect(screen.getByText("Bouncing balls")).toBeTruthy();
    expect(screen.queryByText("Color picker")).toBeNull();

    // Click "animation" again to deactivate filter
    await userEvent.click(screen.getByRole("button", { name: "animation" }));

    // Both templates visible again
    expect(screen.getByText("Bouncing balls")).toBeTruthy();
    expect(screen.getByText("Color picker")).toBeTruthy();
  });
});
