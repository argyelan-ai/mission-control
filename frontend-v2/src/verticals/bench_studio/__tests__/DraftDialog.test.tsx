import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { BenchChallenge } from "../types";

vi.mock("@/verticals/bench_studio/api", () => ({
  benchApi: {
    challenges: {
      list: vi.fn(), get: vi.fn(), create: vi.fn(),
      draft: vi.fn().mockResolvedValue({
        approval_id: "ap-1", challenge_status: "drafted", warnings: [],
      }),
      rerender: vi.fn(),
    },
    entries: { retry: vi.fn() },
    promptTemplates: { list: vi.fn() },
    sharedSubpath: (p: string) => p,
  },
}));

import { benchApi } from "@/verticals/bench_studio/api";
import { DraftDialog } from "../DraftDialog";

const CHALLENGE = {
  id: "ch-1", title: "T", status: "review", mode: "side_by_side",
  composed_video_path: "/sd/grid.mp4", entries: [],
} as unknown as BenchChallenge;

function renderDialog() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <DraftDialog challenge={CHALLENGE} open onClose={() => {}} />
    </QueryClientProvider>
  );
}

describe("DraftDialog", () => {
  it("shows a live character counter", async () => {
    renderDialog();
    const textarea = screen.getByRole("textbox");
    await userEvent.type(textarea, "hello");
    expect(screen.getByText("5/280")).toBeTruthy();
  });

  it("disables submit when empty and when over 280 chars", async () => {
    renderDialog();
    const submit = screen.getByRole("button", { name: /Draft erstellen/ });
    expect(submit).toHaveProperty("disabled", true);

    const textarea = screen.getByRole("textbox");
    await userEvent.click(textarea);
    await userEvent.paste("x".repeat(281));
    expect(screen.getByText("281/280")).toBeTruthy();
    expect(submit).toHaveProperty("disabled", true);
  });

  it("submits tweet text + speed-labels toggle", async () => {
    renderDialog();
    await userEvent.type(screen.getByRole("textbox"), "great duel");
    await userEvent.click(screen.getByRole("checkbox"));
    await userEvent.click(screen.getByRole("button", { name: /Draft erstellen/ }));
    expect(benchApi.challenges.draft).toHaveBeenCalledWith("ch-1", {
      tweet_text: "great duel",
      include_speed_labels: true,
    });
  });
});
