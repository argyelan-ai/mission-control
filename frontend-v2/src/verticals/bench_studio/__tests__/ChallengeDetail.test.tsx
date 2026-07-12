import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
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
      stop: vi.fn(),
      archive: vi.fn(),
      unarchive: vi.fn(),
      remove: vi.fn(),
    },
    entries: { retry: vi.fn() },
    promptTemplates: { list: vi.fn().mockResolvedValue([]) },
    sharedSubpath: (p: string) => p.replace(/^\/shared-deliverables\//, ""),
  },
}));

vi.mock("@/lib/api", () => ({
  api: { files: { contentUrl: (root: string, sub: string) => `/files/${root}/${sub}` } },
  getToken: () => "test-token",
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

function makeChallenge(over: Partial<BenchChallenge> = {}): BenchChallenge {
  return {
    id: "ch-1",
    title: "Bouncing balls",
    prompt_template_id: null,
    prompt_text: "100 balls",
    mode: "side_by_side",
    status: "review",
    series_label: null,
    series_no: null,
    composed_video_path: null,
    content_pipeline_id: null,
    error: null,
    archived_at: null,
    created_at: "2026-07-11T10:00:00Z",
    updated_at: "2026-07-11T10:00:00Z",
    entries: [],
    ...over,
  };
}

function renderDetail(onBack = () => {}) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <ChallengeDetail challengeId="ch-1" onBack={onBack} />
    </QueryClientProvider>
  );
}

describe("ChallengeDetail — stop / archive / delete", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("shows Stoppen on a running challenge and calls stop", async () => {
    const running = makeChallenge({ status: "generating" });
    vi.mocked(benchApi.challenges.get).mockResolvedValue(running);
    vi.mocked(benchApi.challenges.stop).mockResolvedValue(
      makeChallenge({ status: "failed", error: "stopped by operator" })
    );

    renderDetail();
    const stopBtn = await screen.findByRole("button", { name: /Stoppen/ });
    await userEvent.click(stopBtn);
    await waitFor(() => {
      expect(benchApi.challenges.stop).toHaveBeenCalledWith("ch-1");
    });
    // Running challenges cannot be archived or deleted:
    expect(screen.queryByRole("button", { name: /Archivieren/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /Challenge löschen/ })).toBeNull();
  });

  it("archives a finished challenge", async () => {
    vi.mocked(benchApi.challenges.get).mockResolvedValue(makeChallenge({ status: "review" }));
    vi.mocked(benchApi.challenges.archive).mockResolvedValue(
      makeChallenge({ archived_at: "2026-07-12T10:00:00Z" })
    );

    renderDetail();
    // No stop button on finished challenges:
    expect(screen.queryByRole("button", { name: /Stoppen/ })).toBeNull();
    const archiveBtn = await screen.findByRole("button", { name: /Archivieren/ });
    await userEvent.click(archiveBtn);
    await waitFor(() => {
      expect(benchApi.challenges.archive).toHaveBeenCalledWith("ch-1");
    });
  });

  it("unarchives an archived challenge", async () => {
    vi.mocked(benchApi.challenges.get).mockResolvedValue(
      makeChallenge({ status: "review", archived_at: "2026-07-12T10:00:00Z" })
    );
    vi.mocked(benchApi.challenges.unarchive).mockResolvedValue(makeChallenge());

    renderDetail();
    const btn = await screen.findByRole("button", { name: /Entarchivieren/ });
    await userEvent.click(btn);
    await waitFor(() => {
      expect(benchApi.challenges.unarchive).toHaveBeenCalledWith("ch-1");
    });
  });

  it("deletes via confirm dialog (no window.confirm) and navigates back", async () => {
    vi.mocked(benchApi.challenges.get).mockResolvedValue(makeChallenge({ status: "failed" }));
    vi.mocked(benchApi.challenges.remove).mockResolvedValue(undefined);
    const onBack = vi.fn();

    renderDetail(onBack);
    const trashBtn = await screen.findByRole("button", { name: /Challenge löschen/ });
    await userEvent.click(trashBtn);
    // Nothing deleted yet — the confirm dialog is up:
    expect(benchApi.challenges.remove).not.toHaveBeenCalled();
    expect(await screen.findByText("Challenge löschen?")).toBeTruthy();

    await userEvent.click(screen.getByRole("button", { name: /^Löschen$/ }));
    await waitFor(() => {
      expect(benchApi.challenges.remove).toHaveBeenCalledWith("ch-1");
      expect(onBack).toHaveBeenCalled();
    });
  });

  it("cancel in the confirm dialog does not delete", async () => {
    vi.mocked(benchApi.challenges.get).mockResolvedValue(makeChallenge({ status: "failed" }));

    renderDetail();
    await userEvent.click(await screen.findByRole("button", { name: /Challenge löschen/ }));
    await screen.findByText("Challenge löschen?");
    await userEvent.click(screen.getByRole("button", { name: /Abbrechen/ }));
    expect(benchApi.challenges.remove).not.toHaveBeenCalled();
  });
});
