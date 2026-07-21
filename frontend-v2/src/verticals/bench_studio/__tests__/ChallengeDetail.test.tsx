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
import { request } from "@/lib/api";
import { notify } from "@/lib/notify";
import { ChallengeDetail, isSafeChallengeActionEndpoint } from "../ChallengeDetail";

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
    record_duration_s: null,
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

// ── edit + recompose (2026-07-12) ─────────────────────────────────────────

import type { BenchEntry } from "../types";

function makeEntry(over: Partial<BenchEntry> = {}): BenchEntry {
  return {
    id: "e-1",
    challenge_id: "ch-1",
    model_label: "Qwen 3.6",
    source_kind: "spark",
    spark_model: null,
    agent_id: null,
    display_tag: null,
    task_id: null,
    status: "rendered",
    artifact_path: null,
    video_path: "/sd/a.mp4",
    screenshot_path: null,
    metrics: {},
    error: null,
    ...over,
  };
}

describe("ChallengeDetail — edit + recompose", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("'Video neu erstellen' calls recompose when 2 recordings exist", async () => {
    vi.mocked(benchApi.challenges.get).mockResolvedValue(
      makeChallenge({
        entries: [
          makeEntry({ id: "e-1", video_path: "/sd/a.mp4" }),
          makeEntry({ id: "e-2", model_label: "Grok", video_path: "/sd/b.mp4" }),
        ],
      })
    );
    vi.mocked(benchApi.challenges.recompose).mockResolvedValue({ ok: true });

    renderDetail();
    const btn = await screen.findByRole("button", { name: /Video neu erstellen/ });
    await userEvent.click(btn);
    await waitFor(() => {
      expect(benchApi.challenges.recompose).toHaveBeenCalledWith("ch-1");
    });
  });

  it("hides recompose with no recordings at all", async () => {
    vi.mocked(benchApi.challenges.get).mockResolvedValue(
      makeChallenge({ entries: [makeEntry({ video_path: null })] })
    );
    renderDetail();
    await screen.findByRole("button", { name: /Challenge bearbeiten/ });
    expect(screen.queryByRole("button", { name: /Video neu erstellen/ })).toBeNull();
  });

  // Single-video-branding (2026-07-13): the backend now composes a branded
  // solo frame from just 1 recording, for "single" mode AND for a
  // side_by_side run that degraded to 1 survivor — so the button must show
  // for both, not just the 2-entry side_by_side case.
  it("'Video neu erstellen' calls recompose for a single-mode challenge with 1 recording", async () => {
    vi.mocked(benchApi.challenges.get).mockResolvedValue(
      makeChallenge({
        mode: "single",
        entries: [makeEntry({ id: "e-1", video_path: "/sd/a.mp4" })],
      })
    );
    vi.mocked(benchApi.challenges.recompose).mockResolvedValue({ ok: true });

    renderDetail();
    const btn = await screen.findByRole("button", { name: /Video neu erstellen/ });
    await userEvent.click(btn);
    await waitFor(() => {
      expect(benchApi.challenges.recompose).toHaveBeenCalledWith("ch-1");
    });
  });

  it("shows recompose for a side_by_side challenge degraded to 1 surviving recording", async () => {
    vi.mocked(benchApi.challenges.get).mockResolvedValue(
      makeChallenge({
        mode: "side_by_side",
        entries: [
          makeEntry({ id: "e-1", video_path: "/sd/a.mp4" }),
          makeEntry({ id: "e-2", model_label: "Grok", status: "failed", video_path: null }),
        ],
      })
    );
    renderDetail();
    await screen.findByRole("button", { name: /Video neu erstellen/ });
  });

  it("labels the composed video 'Benchmark-Video' for single mode, 'Grid-Video' for side_by_side", async () => {
    vi.mocked(benchApi.challenges.get).mockResolvedValue(
      makeChallenge({
        mode: "single",
        composed_video_path: "/sd/branded-solo.mp4",
        entries: [makeEntry({ id: "e-1", video_path: "/sd/a.mp4" })],
      })
    );
    renderDetail();
    await screen.findByText("Benchmark-Video");
    expect(screen.queryByText("Grid-Video")).toBeNull();
  });

  it("edit dialog saves title and changed entry fields only", async () => {
    vi.mocked(benchApi.challenges.get).mockResolvedValue(
      makeChallenge({
        entries: [
          makeEntry({ id: "e-1", model_label: "Qwen 3.6", display_tag: null }),
          makeEntry({ id: "e-2", model_label: "Grok 4.5", video_path: "/sd/b.mp4" }),
        ],
      })
    );
    vi.mocked(benchApi.challenges.update).mockResolvedValue(makeChallenge());
    vi.mocked(benchApi.entries.update).mockResolvedValue(makeEntry());

    renderDetail();
    await userEvent.click(await screen.findByRole("button", { name: /Challenge bearbeiten/ }));

    // Change the title:
    const titleInput = await screen.findByRole("textbox", { name: /Titel/ });
    await userEvent.clear(titleInput);
    await userEvent.type(titleInput, "Better Title");

    // Change entry 1's label + tag; leave entry 2 untouched:
    const labelInput = screen.getByRole("textbox", { name: /Modell-Name 1/ });
    await userEvent.clear(labelInput);
    await userEvent.type(labelInput, "Qwen 3.6 35B A3B");
    const tagInput = screen.getByRole("textbox", { name: /^Tag 1$/ });
    await userEvent.type(tagInput, "OMP · DGX SPARK");

    await userEvent.click(screen.getByRole("button", { name: /Speichern/ }));

    await waitFor(() => {
      expect(benchApi.challenges.update).toHaveBeenCalledWith("ch-1", {
        title: "Better Title",
      });
      expect(benchApi.entries.update).toHaveBeenCalledWith("e-1", {
        model_label: "Qwen 3.6 35B A3B",
        display_tag: "OMP · DGX SPARK",
      });
    });
    // Unchanged entry is not PATCHed:
    expect(benchApi.entries.update).toHaveBeenCalledTimes(1);
  });
});

// ── grid-video spinner + cache-staleness (2026-07-13) ──────────────────────

describe("ChallengeDetail — grid video spinner", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("shows a spinner instead of the video while composing, even with a stale composed_video_path", async () => {
    vi.mocked(benchApi.challenges.get).mockResolvedValue(
      makeChallenge({
        status: "composing",
        composed_video_path: "/shared-deliverables/bench-x/grid-old.mp4",
        entries: [
          makeEntry({ id: "e-1", video_path: "/sd/a.mp4" }),
          makeEntry({ id: "e-2", model_label: "Grok", video_path: "/sd/b.mp4" }),
        ],
      })
    );

    renderDetail();
    expect(await screen.findByText("Video wird zusammengesetzt…")).toBeTruthy();
    // The stale video must NOT be rendered while composing:
    expect(screen.queryByText("/shared-deliverables/bench-x/grid-old.mp4")).toBeNull();
  });

  it("shows a spinner while rendering", async () => {
    vi.mocked(benchApi.challenges.get).mockResolvedValue(
      makeChallenge({ status: "rendering", entries: [makeEntry()] })
    );
    renderDetail();
    expect(await screen.findByText("Aufnahmen werden gerendert…")).toBeTruthy();
  });

  it("shows the composed video once review is reached", async () => {
    vi.mocked(benchApi.challenges.get).mockResolvedValue(
      makeChallenge({
        status: "review",
        composed_video_path: "/shared-deliverables/bench-x/grid-abc123.mp4",
        entries: [makeEntry({ id: "e-1" }), makeEntry({ id: "e-2", model_label: "Grok" })],
      })
    );
    renderDetail();
    expect(
      await screen.findByText("/shared-deliverables/bench-x/grid-abc123.mp4")
    ).toBeTruthy();
    expect(screen.queryByText("Video wird zusammengesetzt…")).toBeNull();
  });

  it("shows a per-entry spinner while an entry is still generating", async () => {
    vi.mocked(benchApi.challenges.get).mockResolvedValue(
      makeChallenge({
        status: "generating",
        entries: [
          makeEntry({ id: "e-1", status: "generating", video_path: null }),
          makeEntry({ id: "e-2", model_label: "Grok", status: "pending", video_path: null }),
        ],
      })
    );
    renderDetail();
    expect(await screen.findByText("Wird generiert…")).toBeTruthy();
    expect(await screen.findByText("Wartet…")).toBeTruthy();
  });
});

// ── "Öffnen" button (view rendered artifact as a real page) ───────────────
//
// Click-based, not a plain <a href>: the URL carries a short-lived,
// resource-scoped view-token (never the operator's session JWT — that would
// be a standing credential leak once the copyable/shareable link is opened
// on a phone or lands in browser history). The token is minted lazily on
// click via entries.viewToken().
//
// window.open() itself must happen SYNCHRONOUSLY inside the click handler —
// Safari/iOS only waives the popup blocker within the same tick as the user
// gesture. The token is minted afterwards and the already-open blank tab is
// redirected via tab.location.href (standard "open blank, fill in later"
// pattern) — a naive `await mint(); window.open(url)` gets silently blocked.

describe("ChallengeDetail — Öffnen button", () => {
  let fakeTab: { location: { href: string }; close: ReturnType<typeof vi.fn>; closed: boolean };

  beforeEach(() => {
    vi.clearAllMocks();
    fakeTab = { location: { href: "" }, close: vi.fn(), closed: false };
    vi.stubGlobal("open", vi.fn(() => fakeTab));
  });

  it("opens a blank tab synchronously, then redirects it once the view-token is minted", async () => {
    vi.mocked(benchApi.challenges.get).mockResolvedValue(
      makeChallenge({
        entries: [makeEntry({ id: "e-1", artifact_path: "/shared-deliverables/bench-ch-1/A/index.html" })],
      })
    );
    vi.mocked(benchApi.entries.viewToken).mockResolvedValue({
      token: "scoped-view-token",
      expires_in: 1800,
    });

    renderDetail();
    const btn = await screen.findByRole("button", { name: /Öffnen/ });
    await userEvent.click(btn);

    // Blank tab opened right away — valid windowFeatures token only:
    expect(window.open).toHaveBeenCalledWith("", "_blank", "noopener");

    await waitFor(() => {
      expect(benchApi.entries.viewToken).toHaveBeenCalledWith("ch-1", "e-1");
    });
    await waitFor(() => {
      expect(fakeTab.location.href).toBe(
        "/api/v1/bench/challenges/ch-1/entries/e-1/view?token=scoped-view-token"
      );
    });
  });

  it("falls back to same-tab navigation when the popup blocker returns null", async () => {
    vi.mocked(window.open).mockReturnValue(null);
    const originalLocation = window.location;
    // jsdom's window.location isn't directly assignable — replace it for this test only:
    Object.defineProperty(window, "location", {
      configurable: true,
      value: { ...originalLocation, href: "" },
    });

    vi.mocked(benchApi.challenges.get).mockResolvedValue(
      makeChallenge({
        entries: [makeEntry({ id: "e-1", artifact_path: "/shared-deliverables/bench-ch-1/A/index.html" })],
      })
    );
    vi.mocked(benchApi.entries.viewToken).mockResolvedValue({
      token: "scoped-view-token",
      expires_in: 1800,
    });

    renderDetail();
    const btn = await screen.findByRole("button", { name: /Öffnen/ });
    await userEvent.click(btn);

    await waitFor(() => {
      expect(window.location.href).toBe(
        "/api/v1/bench/challenges/ch-1/entries/e-1/view?token=scoped-view-token"
      );
    });

    Object.defineProperty(window, "location", { configurable: true, value: originalLocation });
  });

  it("closes the blank tab and shows an error when minting the view-token fails", async () => {
    vi.mocked(benchApi.challenges.get).mockResolvedValue(
      makeChallenge({
        entries: [makeEntry({ id: "e-1", artifact_path: "/shared-deliverables/bench-ch-1/A/index.html" })],
      })
    );
    vi.mocked(benchApi.entries.viewToken).mockRejectedValue(new Error("boom"));

    renderDetail();
    const btn = await screen.findByRole("button", { name: /Öffnen/ });
    await userEvent.click(btn);

    await waitFor(() => {
      expect(notify.error).toHaveBeenCalled();
    });
    expect(fakeTab.close).toHaveBeenCalled();
    expect(fakeTab.location.href).toBe("");
  });

  it("does not render 'Öffnen' when the entry has no artifact yet", async () => {
    vi.mocked(benchApi.challenges.get).mockResolvedValue(
      makeChallenge({ entries: [makeEntry({ id: "e-1", artifact_path: null })] })
    );

    renderDetail();
    await screen.findByText("Qwen 3.6");
    expect(screen.queryByRole("button", { name: /Öffnen/ })).toBeNull();
  });
});

// ── per-entry rerender button (2026-07-15) ──────────────────────────────

describe("ChallengeDetail — per-entry rerender", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("shows Rerender for an entry with a recorded artifact and fires the mutation", async () => {
    vi.mocked(benchApi.challenges.get).mockResolvedValue(
      makeChallenge({
        status: "review",
        entries: [
          makeEntry({
            id: "e-1",
            status: "rendered",
            artifact_path: "/shared-deliverables/bench-ch-1/A/index.html",
          }),
        ],
      })
    );
    vi.mocked(benchApi.entries.rerender).mockResolvedValue({ ok: true });

    renderDetail();
    const btn = await screen.findByRole("button", { name: /Qwen 3.6 neu rendern/ });
    await userEvent.click(btn);

    await waitFor(() => {
      expect(benchApi.entries.rerender).toHaveBeenCalledWith("e-1");
    });
    expect(notify.success).toHaveBeenCalled();
  });

  it("hides Rerender for an entry without an artifact_path", async () => {
    vi.mocked(benchApi.challenges.get).mockResolvedValue(
      makeChallenge({
        entries: [makeEntry({ id: "e-1", status: "rendered", artifact_path: null })],
      })
    );
    renderDetail();
    await screen.findByText("Qwen 3.6");
    expect(screen.queryByRole("button", { name: /neu rendern/ })).toBeNull();
  });

  it("hides Rerender while the entry is still generating (no settled status)", async () => {
    vi.mocked(benchApi.challenges.get).mockResolvedValue(
      makeChallenge({
        status: "generating",
        entries: [
          makeEntry({
            id: "e-1",
            status: "generating",
            video_path: null,
            artifact_path: "/shared-deliverables/bench-ch-1/A/index.html",
          }),
        ],
      })
    );
    renderDetail();
    await screen.findByText("Wird generiert…");
    expect(screen.queryByRole("button", { name: /neu rendern/ })).toBeNull();
  });

  it("shows Rerender for a failed entry that still has a recorded artifact", async () => {
    vi.mocked(benchApi.challenges.get).mockResolvedValue(
      makeChallenge({
        status: "review",
        entries: [
          makeEntry({
            id: "e-1",
            status: "failed",
            video_path: null,
            artifact_path: "/shared-deliverables/bench-ch-1/A/index.html",
            error: "render failed: sidecar timeout",
          }),
        ],
      })
    );
    renderDetail();
    expect(await screen.findByRole("button", { name: /Qwen 3.6 neu rendern/ })).toBeTruthy();
  });

  it("shows the in-button spinner once the challenge flips to rendering after the click", async () => {
    const entry = makeEntry({
      id: "e-1",
      status: "rendered",
      artifact_path: "/shared-deliverables/bench-ch-1/A/index.html",
    });
    vi.mocked(benchApi.challenges.get)
      .mockResolvedValueOnce(makeChallenge({ status: "review", entries: [entry] }))
      .mockResolvedValue(
        makeChallenge({ status: "rendering", entries: [{ ...entry, status: "generated" }] })
      );
    vi.mocked(benchApi.entries.rerender).mockResolvedValue({ ok: true });

    renderDetail();
    const btn = await screen.findByRole("button", { name: /Qwen 3.6 neu rendern/ });
    await userEvent.click(btn);

    await waitFor(() => {
      expect(benchApi.challenges.get).toHaveBeenCalledTimes(2);
    });
    // Once the poll picks up 'rendering', the button carries a spinner and
    // is disabled — consistent with the existing isRunning convention.
    await waitFor(() => {
      const rerenderBtn = screen.getByRole("button", { name: /Qwen 3.6 neu rendern/ });
      expect(rerenderBtn).toBeDisabled();
    });
  });

  it("shows the backend's cooldown message on a 429 and does not leave the button spinning", async () => {
    vi.mocked(benchApi.challenges.get).mockResolvedValue(
      makeChallenge({
        status: "review",
        entries: [
          makeEntry({
            id: "e-1",
            status: "rendered",
            artifact_path: "/shared-deliverables/bench-ch-1/A/index.html",
          }),
        ],
      })
    );
    vi.mocked(benchApi.entries.rerender).mockRejectedValue(
      new Error(
        'API 429: {"detail":"Rerender already running for this entry — try again in 42s."}'
      )
    );

    renderDetail();
    const btn = await screen.findByRole("button", { name: /Qwen 3.6 neu rendern/ });
    await userEvent.click(btn);

    await waitFor(() => {
      expect(notify.error).toHaveBeenCalledWith(
        "Rerender already running for this entry — try again in 42s."
      );
    });
    // Challenge status never left 'review' — the button must not stay
    // spinning after a rejected request.
    expect(screen.getByRole("button", { name: /Qwen 3.6 neu rendern/ })).not.toBeDisabled();
  });
});

describe("ChallengeDetail — extension-point actions (ADR-044)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders no extra buttons when the challenge has no actions", async () => {
    vi.mocked(benchApi.challenges.get).mockResolvedValue(
      makeChallenge({ status: "review", actions: [] })
    );
    renderDetail();
    await screen.findByRole("button", { name: /Draft erstellen/ });
    expect(screen.queryByRole("button", { name: "Publish" })).toBeNull();
  });

  it("renders a registered action button and POSTs to its endpoint on click", async () => {
    vi.mocked(benchApi.challenges.get).mockResolvedValue(
      makeChallenge({
        status: "review",
        actions: [
          {
            id: "catalog-publish",
            label: "Publish",
            style: "primary",
            method: "POST",
            endpoint: "/api/v1/catalog/ch-1/publish",
            confirm: null,
            disabled: false,
            disabled_reason: null,
            busy: false,
          },
        ],
      })
    );
    vi.mocked(request).mockResolvedValue({ ok: true });

    renderDetail();
    const btn = await screen.findByRole("button", { name: "Publish" });
    await userEvent.click(btn);

    await waitFor(() => {
      expect(request).toHaveBeenCalledWith("/api/v1/catalog/ch-1/publish", { method: "POST" });
    });
    await waitFor(() => {
      expect(notify.success).toHaveBeenCalledWith("Publish gestartet");
    });
  });

  it("disables the button and shows the disabled_reason as a title when disabled", async () => {
    vi.mocked(benchApi.challenges.get).mockResolvedValue(
      makeChallenge({
        status: "review",
        actions: [
          {
            id: "catalog-publish",
            label: "Publish",
            style: "primary",
            method: "POST",
            endpoint: "/api/v1/catalog/ch-1/publish",
            confirm: null,
            disabled: true,
            disabled_reason: "Kein Video vorhanden",
            busy: false,
          },
        ],
      })
    );

    renderDetail();
    const btn = await screen.findByRole("button", { name: "Publish" });
    expect(btn).toBeDisabled();
    expect(btn).toHaveAttribute("title", "Kein Video vorhanden");
  });

  it("asks for confirmation before firing when confirm is set, and skips the call on cancel", async () => {
    vi.mocked(benchApi.challenges.get).mockResolvedValue(
      makeChallenge({
        status: "review",
        actions: [
          {
            id: "catalog-unpublish",
            label: "Unpublish",
            style: "danger",
            method: "POST",
            endpoint: "/api/v1/catalog/ch-1/unpublish",
            confirm: "Wirklich vom Katalog entfernen?",
            disabled: false,
            disabled_reason: null,
            busy: false,
          },
        ],
      })
    );
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);

    renderDetail();
    const btn = await screen.findByRole("button", { name: "Unpublish" });
    await userEvent.click(btn);

    expect(confirmSpy).toHaveBeenCalledWith("Wirklich vom Katalog entfernen?");
    expect(request).not.toHaveBeenCalled();
    confirmSpy.mockRestore();
  });

  it("shows a spinner and disables the button while busy=true", async () => {
    vi.mocked(benchApi.challenges.get).mockResolvedValue(
      makeChallenge({
        status: "review",
        actions: [
          {
            id: "catalog-publish",
            label: "Publish",
            style: "primary",
            method: "POST",
            endpoint: "/api/v1/catalog/ch-1/publish",
            confirm: null,
            disabled: false,
            disabled_reason: null,
            busy: true,
          },
        ],
      })
    );

    renderDetail();
    const btn = await screen.findByRole("button", { name: "Publish" });
    expect(btn).toBeDisabled();
  });

  it("does not render a button for an unsafe absolute/protocol-relative endpoint (F3)", async () => {
    vi.mocked(benchApi.challenges.get).mockResolvedValue(
      makeChallenge({
        status: "review",
        actions: [
          {
            id: "evil-absolute",
            label: "Evil Absolute",
            style: "primary",
            method: "POST",
            endpoint: "https://evil.example.com/steal",
            confirm: null,
            disabled: false,
            disabled_reason: null,
            busy: false,
          },
          {
            id: "evil-protocol-relative",
            label: "Evil Protocol Relative",
            style: "primary",
            method: "POST",
            endpoint: "//evil.example.com/steal",
            confirm: null,
            disabled: false,
            disabled_reason: null,
            busy: false,
          },
          {
            id: "safe-one",
            label: "Safe One",
            style: "primary",
            method: "POST",
            endpoint: "/api/v1/catalog/ch-1/publish",
            confirm: null,
            disabled: false,
            disabled_reason: null,
            busy: false,
          },
        ],
      })
    );

    renderDetail();
    await screen.findByRole("button", { name: "Safe One" });
    expect(screen.queryByRole("button", { name: "Evil Absolute" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Evil Protocol Relative" })).toBeNull();
  });

  it("keeps the button disabled/spinning until the post-action refetch has actually landed (F8)", async () => {
    let resolveRefetch: (v: BenchChallenge) => void = () => {};
    const refetchPromise = new Promise<BenchChallenge>((resolve) => {
      resolveRefetch = resolve;
    });

    const withAction = makeChallenge({
      status: "review",
      actions: [
        {
          id: "catalog-publish",
          label: "Publish",
          style: "primary",
          method: "POST",
          endpoint: "/api/v1/catalog/ch-1/publish",
          confirm: null,
          disabled: false,
          disabled_reason: null,
          busy: false,
        },
      ],
    });

    vi.mocked(benchApi.challenges.get)
      .mockResolvedValueOnce(withAction) // initial mount fetch
      .mockReturnValueOnce(refetchPromise); // refetch triggered by invalidateQueries after the POST
    vi.mocked(request).mockResolvedValue({ ok: true });

    renderDetail();
    const btn = await screen.findByRole("button", { name: "Publish" });
    await userEvent.click(btn);

    // The POST resolved (request() was called), but the refetch it
    // triggers hasn't landed yet — the button must stay disabled through
    // that gap, not just until the POST itself settles.
    await waitFor(() => {
      expect(request).toHaveBeenCalledWith("/api/v1/catalog/ch-1/publish", { method: "POST" });
    });
    expect(screen.getByRole("button", { name: "Publish" })).toBeDisabled();

    resolveRefetch(withAction);

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Publish" })).not.toBeDisabled();
    });
  });
});

describe("isSafeChallengeActionEndpoint (F3)", () => {
  it("accepts a same-origin absolute path", () => {
    expect(isSafeChallengeActionEndpoint("/api/v1/catalog/ch-1/publish")).toBe(true);
  });

  it("rejects a full https URL", () => {
    expect(isSafeChallengeActionEndpoint("https://evil.example.com/steal")).toBe(false);
  });

  it("rejects a full http URL", () => {
    expect(isSafeChallengeActionEndpoint("http://evil.example.com/steal")).toBe(false);
  });

  it("rejects a protocol-relative URL", () => {
    expect(isSafeChallengeActionEndpoint("//evil.example.com/steal")).toBe(false);
  });

  it("rejects a relative path without a leading slash", () => {
    expect(isSafeChallengeActionEndpoint("api/v1/catalog/ch-1/publish")).toBe(false);
  });

  it("rejects an empty string", () => {
    expect(isSafeChallengeActionEndpoint("")).toBe(false);
  });
});
