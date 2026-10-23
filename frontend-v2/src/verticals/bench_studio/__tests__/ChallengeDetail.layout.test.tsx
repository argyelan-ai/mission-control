import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { C } from "@/lib/colors";
import type { BenchChallenge, BenchEntry } from "../types";

// Round-1 user test, package P4 ("Bench am Handy"): the detail header put the
// title, the status pill and up to eight buttons into ONE row with the buttons
// marked shrink-0 — on a phone the title shrank to 0 px and the pill slid under
// "Archive". The header is now title row (‹ · title · ⋯), state line, and one
// next-step row; everything else sits behind ⋯ (DESIGN K4/K11/K12). Own file so
// parallel bench PRs do not collide in ChallengeDetail.test.tsx.

vi.mock("@/verticals/bench_studio/api", () => ({
  benchApi: {
    challenges: {
      list: vi.fn(),
      get: vi.fn(),
      create: vi.fn(),
      draft: vi.fn(),
      rerender: vi.fn(),
      stop: vi.fn(),
      archive: vi.fn(),
      unarchive: vi.fn(),
      remove: vi.fn(),
      update: vi.fn(),
      recompose: vi.fn(),
    },
    entries: { retry: vi.fn(), rerender: vi.fn(), update: vi.fn(), viewToken: vi.fn() },
    promptTemplates: { list: vi.fn().mockResolvedValue([]) },
    sharedSubpath: (p: string) => p.replace(/^\/shared-deliverables\//, ""),
    entryViewUrl: (challengeId: string, entryId: string, viewToken: string) =>
      `/api/v1/bench/challenges/${challengeId}/entries/${entryId}/view?token=${viewToken}`,
  },
}));

vi.mock("@/lib/api", () => ({
  api: { files: { contentUrl: (root: string, sub: string) => `/files/${root}/${sub}` } },
  getToken: () => "test-token",
  request: vi.fn(),
}));

vi.mock("@/lib/notify", () => ({
  notify: { success: vi.fn(), error: vi.fn() },
}));

vi.mock("@/components/task/FilePreview", () => ({
  FilePreview: ({ path }: { path: string }) => <div data-testid="file-preview">{path}</div>,
}));

vi.mock("../DraftDialog", () => ({
  DraftDialog: () => null,
}));

import { benchApi } from "@/verticals/bench_studio/api";
import { ChallengeDetail } from "../ChallengeDetail";
import { BENCH_STATUS_COLOR, ENTRY_STATUS_COLOR } from "../ChallengesTab";

function entry(over: Partial<BenchEntry> = {}): BenchEntry {
  return {
    id: "e-1", challenge_id: "ch-1", model_label: "DeepSeek-V4.1", source_kind: "spark",
    spark_model: null, agent_id: null, display_tag: null, task_id: null,
    status: "rendered", artifact_path: "/sd/a/index.html", video_path: "/sd/a.mp4",
    screenshot_path: null, metrics: {}, error: null, ...over,
  };
}

function challenge(over: Partial<BenchChallenge> = {}): BenchChallenge {
  return {
    id: "ch-1", title: "Shot Sequence – a long title that must stay readable on a phone",
    prompt_template_id: null, prompt_text: "p", mode: "side_by_side", status: "review",
    series_label: null, series_no: null, record_duration_s: null,
    composed_video_path: "/sd/grid.mp4", content_pipeline_id: null, error: null,
    archived_at: null, created_at: "2026-10-22T22:19:00Z", updated_at: "2026-10-22T22:19:00Z",
    entries: [entry(), entry({ id: "e-2", model_label: "GLM" })],
    ...over,
  };
}

function renderDetail() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ChallengeDetail challengeId="ch-1" onBack={() => {}} />
    </QueryClientProvider>,
  );
}

describe("ChallengeDetail — header on a phone (P4, t2 MUSS-2 / r2 M2)", () => {
  beforeEach(() => vi.clearAllMocks());

  it("gives the title its own row: only back and ⋯ share the context row above it, no button bar", async () => {
    vi.mocked(benchApi.challenges.get).mockResolvedValue(challenge());
    renderDetail();
    const heading = await screen.findByRole("heading", { level: 1 });
    const context = screen.getByTestId("bench-detail-context");
    const names = within(context)
      .getAllByRole("button")
      .map((b) => b.getAttribute("aria-label") ?? b.textContent);
    expect(names).toEqual(["Challenges", "More actions"]);
    // The title may wrap (K1: cut after 3 lines), it is never squeezed to 0.
    expect(heading.className).toContain("line-clamp-3");
    expect(heading.className).not.toContain("truncate");
    // The status pill is NOT next to the title (it slid under the buttons once).
    expect(within(context).queryByText("Done")).toBeNull();
    expect(heading.textContent).not.toContain("Done");
    expect(screen.getByText("Done")).toBeTruthy();
  });

  it("shows one next step (Create draft) and moves the rest behind ⋯", async () => {
    vi.mocked(benchApi.challenges.get).mockResolvedValue(challenge());
    renderDetail();
    await screen.findByRole("button", { name: /Create draft/ });
    expect(screen.queryByRole("button", { name: /^Archive$/ })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "More actions" }));
    const items = screen.getAllByRole("menuitem").map((i) => i.textContent);
    expect(items).toEqual(["Archive", "Edit", "Rebuild video", "Re-record all", "Delete"]);
  });

  it("while running: Stop is the one action, nothing that cannot work is shown", async () => {
    vi.mocked(benchApi.challenges.get).mockResolvedValue(
      challenge({ status: "generating", composed_video_path: null,
        entries: [entry({ status: "generating", video_path: null, artifact_path: null })] }),
    );
    renderDetail();
    await screen.findByRole("button", { name: /Stop/ });
    expect(screen.queryByRole("button", { name: /Create draft/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /Re-record all/ })).toBeNull();
  });

  it("an overlay action (e.g. publish as episode) is disabled while the challenge runs (t2 KANN-4)", async () => {
    vi.mocked(benchApi.challenges.get).mockResolvedValue(
      challenge({
        status: "generating", composed_video_path: null,
        entries: [entry({ status: "generating", video_path: null, artifact_path: null })],
        actions: [{ id: "publish", label: "Publish as episode", method: "POST",
          endpoint: "/api/v1/x/publish", style: "primary", confirm: null, disabled: false,
          disabled_reason: null, busy: false }],
      }),
    );
    renderDetail();
    expect(await screen.findByRole("button", { name: "Publish as episode" })).toBeDisabled();
  });
});

describe("ChallengeDetail — entry words and numbers (P4, t2 SOLL-3 / KANN-1, r2 S4/S5)", () => {
  beforeEach(() => vi.clearAllMocks());

  it("entry pills use i18n words, not the API codes", async () => {
    vi.mocked(benchApi.challenges.get).mockResolvedValue(
      challenge({ entries: [entry({ status: "rendered" }), entry({ id: "e-2", model_label: "GLM", status: "generated" })] }),
    );
    renderDetail();
    expect(await screen.findByText("Recorded")).toBeTruthy();
    expect(screen.getByText("Generated")).toBeTruthy();
    expect(screen.queryByText("rendered")).toBeNull();
    expect(screen.queryByText("generated")).toBeNull();
    expect(screen.getAllByText("Direct API").length).toBeGreaterThan(0);
    expect(screen.queryByText(/vanilla/i)).toBeNull();
  });

  it("a known entry error is said in the UI language", async () => {
    vi.mocked(benchApi.challenges.get).mockResolvedValue(
      challenge({ status: "review", entries: [
        entry(),
        entry({ id: "e-2", source_kind: "agent", agent_id: "a-1", status: "failed",
          video_path: null, artifact_path: null, error: "agent task deleted" }),
      ] }),
    );
    renderDetail();
    expect(await screen.findByText("Aborted – task deleted")).toBeTruthy();
    expect(screen.queryByText("agent task deleted")).toBeNull();
  });

  it("one word per action: Re-record / Try again, no Rerender / Retry", async () => {
    vi.mocked(benchApi.challenges.get).mockResolvedValue(
      challenge({ entries: [entry(), entry({ id: "e-2", status: "failed", error: "x" })] }),
    );
    renderDetail();
    await screen.findByText("Try again");
    expect(screen.getAllByText("Re-record").length).toBeGreaterThan(0);
    expect(screen.getByText("Try again")).toBeTruthy();
    expect(screen.queryByText("Rerender")).toBeNull();
    expect(screen.queryByText("Retry")).toBeNull();
  });

  it("durations in the duration form, token counts in one number style", async () => {
    vi.mocked(benchApi.challenges.get).mockResolvedValue(
      challenge({ entries: [
        entry({ metrics: { duration_ms: 532_000, tok_per_s: 74, tokens_in: 111, tokens_out: 39_200 } }),
        entry({ id: "e-2", model_label: "GLM", metrics: { duration_ms: 5_540_000 } }),
      ] }),
    );
    renderDetail();
    expect(await screen.findByText("8 min 52 s · 74 tok/s · 111 → 39,200 tok · local")).toBeTruthy();
    expect(screen.getByText("1 h 32 min · local")).toBeTruthy();
  });
});

describe("Bench status colours (P4, t4 N6 / r2 S4)", () => {
  it("a finished challenge waiting for the operator is the accent, never the warning ochre", () => {
    expect(BENCH_STATUS_COLOR.review).toBe(C.accent);
    expect(BENCH_STATUS_COLOR.review).not.toBe(C.warning);
  });

  it("a generated entry is not shown as a warning", () => {
    expect(ENTRY_STATUS_COLOR.generated).not.toBe(C.warning);
  });
});

describe("ChallengeDetail — a link to a challenge that is gone (P4 review SOLL-1)", () => {
  beforeEach(() => vi.clearAllMocks());

  function renderWith(onBack: () => void) {
    // retryDelay 0: a non-404 error still retries (ChallengeDetail's own
    // retry rule), the test need not wait seconds for it.
    const qc = new QueryClient({ defaultOptions: { queries: { retryDelay: 0 } } });
    return render(
      <QueryClientProvider client={qc}>
        <ChallengeDetail challengeId="gone" onBack={onBack} />
      </QueryClientProvider>,
    );
  }

  it("a 404 says 'not found' and offers the way back instead of an empty page", async () => {
    vi.mocked(benchApi.challenges.get).mockRejectedValue(new Error('API 404: {"detail":"Challenge not found"}'));
    const onBack = vi.fn();
    renderWith(onBack);
    expect(await screen.findByRole("heading", { name: "Challenge not found" })).toBeTruthy();
    // 404 is final: no retries.
    expect(benchApi.challenges.get).toHaveBeenCalledTimes(1);
    await userEvent.click(screen.getByRole("button", { name: "Back to list" }));
    await userEvent.click(screen.getByRole("button", { name: "Back" }));
    expect(onBack).toHaveBeenCalledTimes(2);
  });

  it("a mangled id (422) is treated as not found too", async () => {
    vi.mocked(benchApi.challenges.get).mockRejectedValue(new Error("API 422: {}"));
    renderWith(() => {});
    expect(await screen.findByRole("heading", { name: "Challenge not found" })).toBeTruthy();
  });

  it("any other error says it could not load, still with the way back", async () => {
    vi.mocked(benchApi.challenges.get).mockRejectedValue(new Error("API 500: boom"));
    renderWith(() => {});
    expect(await screen.findByRole("heading", { name: "Could not load the challenge" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Back to list" })).toBeTruthy();
  });
});
