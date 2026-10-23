import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { BenchChallenge } from "../types";

vi.mock("@/verticals/bench_studio/api", () => ({
  benchApi: {
    sparkModels: {
      get: vi.fn().mockResolvedValue({ reachable: true, models: [], active: null }),
    },
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
  record_duration_s: null,
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
    // Status words come from i18n (K10), not the raw API code:
    expect(screen.getByText("Recording")).toBeTruthy();
    expect(screen.queryByText("rendering")).toBeNull();
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
    expect(await screen.findByText(/No challenges yet/)).toBeTruthy();
  });

  it("hides archived by default; 'Show archive' re-queries with includeArchived", async () => {
    const user = (await import("@testing-library/user-event")).default;
    renderTab();
    await screen.findByText("Bouncing balls");
    expect(vi.mocked(benchApi.challenges.list)).toHaveBeenCalledWith(false);

    const archived: BenchChallenge = {
      ...CHALLENGE, id: "ch-2", title: "Old run", status: "review",
      archived_at: "2026-07-12T10:00:00Z",
    };
    vi.mocked(benchApi.challenges.list).mockResolvedValue([CHALLENGE, archived]);

    await user.click(screen.getByRole("button", { name: /Show archive/ }));
    expect(await screen.findByText("Old run")).toBeTruthy();
    expect(vi.mocked(benchApi.challenges.list)).toHaveBeenCalledWith(true);
    // Archived row is marked:
    expect(screen.getByText("archived")).toBeTruthy();
  });

  it("shows a healed orphan as 'Aborted – task deleted' without opening it (W0.8)", async () => {
    const orphan: BenchChallenge = {
      ...CHALLENGE, id: "ch-o", title: "Pagoda - OMP", mode: "single", status: "failed",
      series_label: null, series_no: null,
      error: "all entries failed during generation",
      entries: [{
        ...CHALLENGE.entries[1], id: "e-o", challenge_id: "ch-o", model_label: "omp-alpha",
        task_id: null, status: "failed", error: "agent task deleted",
      }],
    };
    vi.mocked(benchApi.challenges.list).mockResolvedValue([orphan]);
    renderTab();
    expect(await screen.findByText("Aborted – task deleted")).toBeTruthy();
    // The generic backend line is replaced by the reason, not shown twice:
    expect(screen.queryByText("all entries failed during generation")).toBeNull();
    expect(vi.mocked(benchApi.challenges.get)).not.toHaveBeenCalled();
  });

  it("keeps the raw error line when the reason is unknown", async () => {
    const odd: BenchChallenge = {
      ...CHALLENGE, id: "ch-x", status: "failed", error: "fan-out crashed — see backend logs",
    };
    vi.mocked(benchApi.challenges.list).mockResolvedValue([odd]);
    renderTab();
    expect(await screen.findByText("Aborted")).toBeTruthy();
    expect(screen.getByText("fan-out crashed — see backend logs")).toBeTruthy();
  });
});

describe("ChallengesTab — list on a phone (P4, t2 SOLL-4)", () => {
  beforeEach(() => {
    vi.mocked(benchApi.challenges.list).mockResolvedValue([CHALLENGE]);
  });

  it("every row names when it was started, so seven equal titles can be told apart", async () => {
    renderTab();
    await screen.findByText("Bouncing balls");
    const when = screen.getByTestId("bench-row-started");
    expect(when.tagName).toBe("TIME");
    expect(when.getAttribute("datetime")).toBe("2026-07-11T10:00:00Z");
    expect(when.textContent).toMatch(/Jul/);
  });

  it("opening a row reports the id, so the page can put it in the URL", async () => {
    const user = (await import("@testing-library/user-event")).default;
    const onSelect = vi.fn();
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <ChallengesTab
          prefillTemplate={null}
          onPrefillConsumed={() => {}}
          selectedId={null}
          onSelectedChange={onSelect}
        />
      </QueryClientProvider>
    );
    await user.click(await screen.findByText("Bouncing balls"));
    expect(onSelect).toHaveBeenCalledWith("ch-1");
  });

  it("the toolbar buttons keep their words on one line and are 44 px tall", async () => {
    renderTab();
    const btn = await screen.findByRole("button", { name: /New Challenge/ });
    expect(btn.className).toContain("whitespace-nowrap");
    expect(btn.className).toContain("min-h-11");
  });
});

describe("ChallengesTab — effort next to the entry name (round-2 Q2)", () => {
  it("two rows of the same model are told apart by the effort word", async () => {
    const base = CHALLENGE.entries[0];
    vi.mocked(benchApi.challenges.list).mockResolvedValue([
      {
        ...CHALLENGE,
        entries: [
          { ...base, id: "e-off", model_label: "DeepSeek-V4.1", metrics: { effort_requested: "off" } },
          { ...base, id: "e-max", model_label: "DeepSeek-V4.1", metrics: { effort_requested: "max" } },
        ],
      },
    ]);
    renderTab();
    expect(await screen.findByText("DeepSeek-V4.1 · Off")).toBeTruthy();
    expect(screen.getByText("DeepSeek-V4.1 · Max")).toBeTruthy();
  });

  it("a row without a chosen effort shows only its name", async () => {
    vi.mocked(benchApi.challenges.list).mockResolvedValue([CHALLENGE]);
    renderTab();
    expect(await screen.findByText("DeepSeek")).toBeTruthy();
  });

  it("the status pill is in sentence case", async () => {
    vi.mocked(benchApi.challenges.list).mockResolvedValue([CHALLENGE]);
    renderTab();
    const pill = await screen.findByText("Recording");
    expect(pill.className).not.toMatch(/uppercase/);
  });
});
