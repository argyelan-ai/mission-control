/**
 * HeadContinueSheet — "Continue" (bauplan `heads-sichtbar` PR 4 §5): a
 * focused sheet, always `mode: "continue"`, no fresh/continue toggle —
 * distinct from the full `HeadRestartDialog` it replaces on a passed
 * head's footer (see that component's own PR 2 note).
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { mkPair, mkRun } from "@/lib/__tests__/headFixtures";
import { HeadContinueSheet } from "../HeadContinueSheet";

const ompLocal = mkPair();
const claudeLocal = mkPair({ harness: "claude", harness_label: "Claude Code", status: "experimental" });

function renderSheet(runOver: Parameters<typeof mkRun>[0] = {}, onRestarted = vi.fn()) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  const onClose = vi.fn();
  render(
    <QueryClientProvider client={qc}>
      <HeadContinueSheet open run={mkRun({ state: "passed", ...runOver })} onClose={onClose} onRestarted={onRestarted} />
    </QueryClientProvider>,
  );
  return { onClose, onRestarted };
}

beforeEach(() => {
  vi.restoreAllMocks();
  vi.spyOn(api.heads, "pairs").mockResolvedValue({ pairs: [ompLocal, claudeLocal], default_pair: ompLocal });
});

describe("HeadContinueSheet", () => {
  it("shows the explain text and a pair picker, no fresh/continue radio", async () => {
    renderSheet();
    await screen.findByTestId("head-continue-explain");
    expect(screen.getByTestId("head-continue-explain")).toHaveTextContent(
      "Same branch — the new run gets the run record plus a summary of the transcript.",
    );
    // No fresh/continue toggle anywhere — distinct from HeadRestartDialog's
    // two radio options.
    expect(screen.queryByTestId("head-restart-mode-continue")).not.toBeInTheDocument();
    expect(screen.queryByTestId("head-restart-mode-fresh")).not.toBeInTheDocument();
    expect(screen.getByTestId("head-continue-note")).toBeInTheDocument();
  });

  it("submits restart with mode=continue and the typed note as the answer", async () => {
    const restart = vi.spyOn(api.heads, "restart").mockResolvedValue({ run_id: "r2", state: "starting", restarted_from: "r1" });
    const { onRestarted, onClose } = renderSheet({ run_id: "r1", harness: "omp", runtime_slug: "glm-local" });
    await screen.findByTestId("head-continue-submit");
    await waitFor(() => expect(screen.getByTestId("head-continue-submit")).not.toBeDisabled());
    await userEvent.type(screen.getByTestId("head-continue-note"), "Also check the edge case with empty input.");
    await userEvent.click(screen.getByTestId("head-continue-submit"));
    await waitFor(() =>
      expect(restart).toHaveBeenCalledWith("r1", {
        harness: "omp", runtime_slug: "glm-local", mode: "continue",
        answer: "Also check the edge case with empty input.",
      }),
    );
    await waitFor(() => expect(onRestarted).toHaveBeenCalledWith("r2"));
    expect(onClose).toHaveBeenCalled();
  });

  it("submits restart with no answer key at all when the note is left empty", async () => {
    const restart = vi.spyOn(api.heads, "restart").mockResolvedValue({ run_id: "r2", state: "starting", restarted_from: "r1" });
    renderSheet({ run_id: "r1", harness: "omp", runtime_slug: "glm-local" });
    await waitFor(() => expect(screen.getByTestId("head-continue-submit")).not.toBeDisabled());
    await userEvent.click(screen.getByTestId("head-continue-submit"));
    await waitFor(() =>
      expect(restart).toHaveBeenCalledWith("r1", { harness: "omp", runtime_slug: "glm-local", mode: "continue" }),
    );
  });

  it("disables submit and shows the no-branch hint when the run never had a branch", async () => {
    // Same `canContinueRun` gate HeadRestartDialog's own "continue" radio
    // uses — a run that ended before the host picked it up cannot continue.
    renderSheet({ reason: "not_picked_up", started_at: null });
    await screen.findByTestId("head-continue-no-branch");
    expect(screen.getByTestId("head-continue-no-branch")).toHaveTextContent(
      "No work on this branch yet — the run ended before it started.",
    );
    expect(screen.getByTestId("head-continue-submit")).toBeDisabled();
  });

  it("lets the operator switch pairs through the normal picker", async () => {
    renderSheet();
    await userEvent.click(await screen.findByTestId("head-pair-trigger"));
    await userEvent.click(await screen.findByRole("option", { name: /Claude Code/i }));
    const restart = vi.spyOn(api.heads, "restart").mockResolvedValue({ run_id: "r2", state: "starting", restarted_from: "r1" });
    await userEvent.click(screen.getByTestId("head-continue-submit"));
    await waitFor(() => expect(restart).toHaveBeenCalledWith(
      expect.any(String),
      expect.objectContaining({ harness: "claude", mode: "continue" }),
    ));
  });
});
