import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { BenchChallenge, BenchEntry, RecordNetworkMetrics } from "../types";

// W0.8 + W0.9 on the detail page: the status names why a challenge ended,
// and an entry says when its recording had network requests blocked or
// carries pasted library code. Own file (same mocks as ChallengeDetail.test)
// so parallel bench PRs do not collide at the end of that file.

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

function entry(over: Partial<BenchEntry> = {}): BenchEntry {
  return {
    id: "e-1", challenge_id: "ch-1", model_label: "DSV4-Flash", source_kind: "spark",
    spark_model: null, agent_id: null, display_tag: null, task_id: null,
    status: "rendered", artifact_path: null, video_path: "/sd/a.mp4", screenshot_path: null,
    metrics: {}, error: null, ...over,
  };
}

const net = (m: RecordNetworkMetrics) => m as BenchEntry["metrics"];

function challenge(over: Partial<BenchChallenge> = {}): BenchChallenge {
  return {
    id: "ch-1", title: "Pagoda Garden", prompt_template_id: null, prompt_text: "p",
    mode: "single", status: "review", series_label: null, series_no: null,
    record_duration_s: null, composed_video_path: null, content_pipeline_id: null,
    error: null, archived_at: null, created_at: "2026-10-19T08:00:00Z",
    updated_at: "2026-10-19T08:00:00Z", entries: [entry()], ...over,
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

describe("ChallengeDetail — status words (W0.8)", () => {
  beforeEach(() => vi.clearAllMocks());

  it("names a deleted task instead of the generic backend line", async () => {
    vi.mocked(benchApi.challenges.get).mockResolvedValue(challenge({
      status: "failed", error: "all entries failed during generation",
      entries: [entry({ source_kind: "agent", status: "failed", video_path: null, error: "agent task deleted" })],
    }));
    renderDetail();
    expect(await screen.findByText("Aborted – task deleted")).toBeTruthy();
    expect(screen.queryByText("all entries failed during generation")).toBeNull();
  });

  it("shows a finished challenge as Done", async () => {
    vi.mocked(benchApi.challenges.get).mockResolvedValue(challenge());
    renderDetail();
    expect(await screen.findByText("Done")).toBeTruthy();
  });
});

describe("ChallengeDetail — recorder network note (W0.9)", () => {
  beforeEach(() => vi.clearAllMocks());

  it("explains a blank recording: requests were blocked", async () => {
    vi.mocked(benchApi.challenges.get).mockResolvedValue(challenge({
      entries: [entry({ metrics: net({ network_blocked: true, network_attempts: 2 }) })],
    }));
    renderDetail();
    expect(
      await screen.findByText("Network blocked while recording: 2 requests stopped — the page tried to load something from the network. A recording may only use the page itself."),
    ).toBeTruthy();
  });

  it("names pasted library code", async () => {
    vi.mocked(benchApi.challenges.get).mockResolvedValue(challenge({
      entries: [entry({ metrics: net({ network_blocked: true, network_attempts: 0, libraries_inlined: ["three.js r160"] }) })],
    }));
    renderDetail();
    expect(await screen.findByText("Contains pasted library code: three.js r160")).toBeTruthy();
    expect(screen.queryByText(/Network blocked while recording/)).toBeNull();
  });

  it("says nothing for a clean recording or one made before the block", async () => {
    vi.mocked(benchApi.challenges.get).mockResolvedValue(challenge({
      entries: [
        entry({ metrics: net({ network_blocked: true, network_attempts: 0, libraries_inlined: [] }) }),
        entry({ id: "e-2", model_label: "Old", metrics: { duration_ms: 1000 } }),
      ],
    }));
    renderDetail();
    await screen.findByText("Old");
    expect(screen.queryByText(/Network blocked/)).toBeNull();
    expect(screen.queryByText(/pasted library/)).toBeNull();
  });
});
