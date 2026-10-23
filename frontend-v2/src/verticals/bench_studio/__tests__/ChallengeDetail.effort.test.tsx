import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { BenchChallenge, BenchEntry } from "../types";

// Round-2 Q2 (operator decision 23.10.2026): the effort is its own field.
// The entry card shows the full label on its own line and the effort as a
// pill next to it (not cut away with the label on a phone, not under the
// video); one word per level from i18n; no wire jargon in the visible text;
// each state once per level ("Generating" not on the challenge, the entry
// pill AND the spinner; "local" not alone and twice); pills in sentence case;
// the detail page starts with its own context row and the title.

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
    id: "ch-1", title: "R2 Effort Off vs Max – Floating Particle",
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


describe("ChallengeDetail — effort as its own field (round-2 Q2)", () => {
  beforeEach(() => vi.clearAllMocks());

  const offMax = () =>
    challenge({
      entries: [
        entry({
          model_label: "DeepSeek-V4.1-Flash-1017",
          metrics: {
            effort_requested: "off",
            effort_sent: { key: "reasoning_effort", value: "none" },
            effort_observed: { thinking: "off", reasoning_tokens: null, reasoning_chars: 0 },
            duration_ms: 95_000, tok_per_s: 37, tokens_in: 111, tokens_out: 3482,
          },
        }),
        entry({
          id: "e-2", model_label: "DeepSeek-V4.1-Flash-1017", video_path: "/sd/b.mp4",
          metrics: {
            effort_requested: "max",
            effort_sent: { key: "reasoning_effort", value: "max" },
            effort_observed: { thinking: "on", reasoning_tokens: 33328, reasoning_chars: 9 },
            duration_ms: 455_000, tok_per_s: 86, tokens_in: 111, tokens_out: 39160,
          },
        }),
      ],
    });

  it("the label is shown whole on its own line, the effort as a pill beside it", async () => {
    vi.mocked(benchApi.challenges.get).mockResolvedValue(offMax());
    renderDetail();
    const cards = await screen.findAllByTestId("bench-entry");
    expect(cards).toHaveLength(2);
    for (const card of cards) {
      const label = within(card).getByTestId("bench-entry-label");
      expect(label.textContent).toBe("DeepSeek-V4.1-Flash-1017");
      expect(label.className).not.toMatch(/truncate/);
    }
    expect(within(cards[0]).getByTestId("bench-entry-effort")).toHaveTextContent("Off");
    expect(within(cards[1]).getByTestId("bench-entry-effort")).toHaveTextContent("Max");
    expect(within(cards[1]).getByTestId("bench-entry-effort")).toHaveAccessibleName("Effort: Max");
  });

  it("the effort sits in the card head, before the video", async () => {
    vi.mocked(benchApi.challenges.get).mockResolvedValue(offMax());
    renderDetail();
    const [card] = await screen.findAllByTestId("bench-entry");
    const pill = within(card).getByTestId("bench-entry-effort");
    const video = within(card).getByTestId("file-preview");
    expect(pill.compareDocumentPosition(video) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    const thinking = within(card).getByText("thinking off");
    expect(thinking.compareDocumentPosition(video) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("no wire jargon in the visible text — it stays in the tooltip", async () => {
    vi.mocked(benchApi.challenges.get).mockResolvedValue(offMax());
    renderDetail();
    const [card] = await screen.findAllByTestId("bench-entry");
    expect(card.textContent).not.toMatch(/reasoning_effort/);
    expect(card.textContent).not.toMatch(/Effort:/);
    expect(within(card).getByTestId("bench-entry-effort").getAttribute("title")).toMatch(
      /reasoning_effort=none/,
    );
  });

  it("a 95-second run says 1 min 35 s, not 1 min", async () => {
    vi.mocked(benchApi.challenges.get).mockResolvedValue(offMax());
    renderDetail();
    expect(await screen.findByText("1 min 35 s · 37 tok/s · 111 → 3,482 tok · local")).toBeTruthy();
    expect(screen.getByText("7 min 35 s · 86 tok/s · 111 → 39,160 tok · local")).toBeTruthy();
  });

  it("the measurement line is not set in mono (K7: durations are not machine values)", async () => {
    vi.mocked(benchApi.challenges.get).mockResolvedValue(offMax());
    renderDetail();
    const line = await screen.findByText(/^1 min 35 s/);
    expect(line.className).not.toMatch(/font-mono/);
  });

  it("a running entry shows its state once: the spinner, no extra pill, no lone 'local'", async () => {
    vi.mocked(benchApi.challenges.get).mockResolvedValue(
      challenge({
        status: "generating",
        composed_video_path: null,
        entries: [
          entry({ status: "generating", video_path: null, artifact_path: null,
                  metrics: { effort_requested: "off" } }),
          entry({ id: "e-2", status: "pending", video_path: null, artifact_path: null,
                  metrics: { effort_requested: "max" } }),
        ],
      }),
    );
    renderDetail();
    const cards = await screen.findAllByTestId("bench-entry");
    expect(within(cards[0]).queryByTestId("bench-entry-status")).toBeNull();
    expect(within(cards[0]).getByText("Generating…")).toBeTruthy();
    expect(within(cards[0]).queryByText("local")).toBeNull();
    // On the page: once as the challenge state, once in the entry's spinner.
    expect(screen.getAllByText(/^Generating/)).toHaveLength(2);
  });

  it("a finished entry keeps its status pill, in sentence case", async () => {
    vi.mocked(benchApi.challenges.get).mockResolvedValue(offMax());
    renderDetail();
    const [card] = await screen.findAllByTestId("bench-entry");
    const pill = within(card).getByTestId("bench-entry-status");
    expect(pill).toHaveTextContent("Recorded");
    expect(pill.className).not.toMatch(/uppercase/);
  });

  it("the page starts with a context row and the title as the one heading", async () => {
    vi.mocked(benchApi.challenges.get).mockResolvedValue(offMax());
    renderDetail();
    const heading = await screen.findByRole("heading", { level: 1 });
    expect(heading).toHaveTextContent("R2 Effort Off vs Max – Floating Particle");
    const context = screen.getByTestId("bench-detail-context");
    expect(within(context).getByRole("button", { name: "Challenges" })).toBeTruthy();
    expect(context.compareDocumentPosition(heading) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });
});
