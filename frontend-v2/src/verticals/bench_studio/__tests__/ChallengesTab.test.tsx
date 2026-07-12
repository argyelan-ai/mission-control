import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { BenchChallenge } from "../types";

vi.mock("@/verticals/bench_studio/api", () => ({
  benchApi: {
    challenges: {
      list: vi.fn(),
      get: vi.fn(),
      create: vi.fn(),
      draft: vi.fn(),
      rerender: vi.fn(),
    },
    entries: { retry: vi.fn() },
    promptTemplates: { list: vi.fn().mockResolvedValue([]) },
    sharedSubpath: (p: string) => p.replace(/^\/shared-deliverables\//, ""),
  },
}));

import { benchApi } from "@/verticals/bench_studio/api";
import { ChallengesTab } from "../ChallengesTab";

const CHALLENGE: BenchChallenge = {
  id: "ch-1",
  title: "Bouncing balls",
  prompt_template_id: null,
  prompt_text: "100 balls",
  mode: "side_by_side",
  status: "rendering",
  series_label: "Spark Bench",
  series_no: 7,
  composed_video_path: null,
  content_pipeline_id: null,
  error: null,
  archived_at: null,
  created_at: "2026-07-11T10:00:00Z",
  updated_at: "2026-07-11T10:00:00Z",
  entries: [
    {
      id: "e-1", challenge_id: "ch-1", model_label: "DeepSeek",
      source_kind: "spark", spark_model: "deepseek-x", agent_id: null, display_tag: null,
      task_id: null, status: "rendered", artifact_path: "/sd/a/index.html",
      video_path: "/sd/a.mp4", screenshot_path: null,
      metrics: { duration_ms: 42000, tok_per_s: 87 }, error: null,
    },
    {
      id: "e-2", challenge_id: "ch-1", model_label: "Claude",
      source_kind: "agent", spark_model: null, agent_id: "a-1", display_tag: null,
      task_id: "t-1", status: "generating", artifact_path: null,
      video_path: null, screenshot_path: null, metrics: {}, error: null,
    },
  ],
};

function renderTab() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <ChallengesTab prefillTemplate={null} onPrefillConsumed={() => {}} />
    </QueryClientProvider>
  );
}

describe("ChallengesTab", () => {
  beforeEach(() => {
    vi.mocked(benchApi.challenges.list).mockResolvedValue([CHALLENGE]);
  });

  it("renders challenge rows with status chip and series label", async () => {
    renderTab();
    expect(await screen.findByText("Bouncing balls")).toBeTruthy();
    expect(screen.getByText("rendering")).toBeTruthy();
    expect(screen.getByText("Spark Bench #7")).toBeTruthy();
  });

  it("renders per-entry progress labels", async () => {
    renderTab();
    expect(await screen.findByText("DeepSeek")).toBeTruthy();
    expect(screen.getByText("Claude")).toBeTruthy();
  });

  it("shows the empty state when there are no challenges", async () => {
    vi.mocked(benchApi.challenges.list).mockResolvedValue([]);
    renderTab();
    expect(await screen.findByText(/Noch keine Challenges/)).toBeTruthy();
  });

  it("hides archived by default; 'Archiv anzeigen' re-queries with includeArchived", async () => {
    const user = (await import("@testing-library/user-event")).default;
    renderTab();
    await screen.findByText("Bouncing balls");
    expect(vi.mocked(benchApi.challenges.list)).toHaveBeenCalledWith(false);

    const archived: BenchChallenge = {
      ...CHALLENGE, id: "ch-2", title: "Old run", status: "review",
      archived_at: "2026-07-12T10:00:00Z",
    };
    vi.mocked(benchApi.challenges.list).mockResolvedValue([CHALLENGE, archived]);

    await user.click(screen.getByRole("button", { name: /Archiv anzeigen/ }));
    expect(await screen.findByText("Old run")).toBeTruthy();
    expect(vi.mocked(benchApi.challenges.list)).toHaveBeenCalledWith(true);
    // Archived row is marked:
    expect(screen.getByText("archiviert")).toBeTruthy();
  });
});
