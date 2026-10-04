/**
 * HeadNoTranscriptFallback — one heading per `reason` (review finding on
 * PR #756 round 3: every reason but `no_reader` used to share one "No
 * output yet." heading, wrong for an ENDED run with no transcript at all),
 * and the `/log` tail shown only when it genuinely has content — never a
 * permanent "…" on a failed fetch, never a second copy of the heading as
 * the log body.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { HeadNoTranscriptFallback } from "../HeadNoTranscriptFallback";

function renderFallback(reason: string | null) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <HeadNoTranscriptFallback runId="run-1" reason={reason} />
    </QueryClientProvider>,
  );
}

beforeEach(() => vi.restoreAllMocks());

describe("HeadNoTranscriptFallback", () => {
  it("not_yet: waiting for the first transcript lines, no 'no output' wording", async () => {
    vi.spyOn(api.heads, "log").mockResolvedValue("");
    renderFallback("not_yet");
    expect(await screen.findByText("Waiting for the first transcript lines …")).toBeInTheDocument();
    expect(screen.queryByText("No output yet.")).not.toBeInTheDocument();
  });

  it("no_transcript: says none was kept — never 'yet' for an ended run", async () => {
    vi.spyOn(api.heads, "log").mockResolvedValue("");
    renderFallback("no_transcript");
    expect(await screen.findByText("No transcript was kept for this run.")).toBeInTheDocument();
    expect(screen.queryByText(/yet/i)).not.toBeInTheDocument();
  });

  it("too_large: says too large to show — never 'yet'", async () => {
    vi.spyOn(api.heads, "log").mockResolvedValue("");
    renderFallback("too_large");
    expect(await screen.findByText("Transcript too large to show.")).toBeInTheDocument();
    expect(screen.queryByText(/yet/i)).not.toBeInTheDocument();
  });

  it("no_reader: keeps its own title + hint, distinct from the other three", async () => {
    vi.spyOn(api.heads, "log").mockResolvedValue("");
    renderFallback("no_reader");
    expect(await screen.findByText("No live transcript for this harness")).toBeInTheDocument();
    expect(screen.getByText("Showing the last log lines instead.")).toBeInTheDocument();
  });

  it("a null reason (meta not resolved yet) reads the same as not_yet", async () => {
    vi.spyOn(api.heads, "log").mockResolvedValue("");
    renderFallback(null);
    expect(await screen.findByText("Waiting for the first transcript lines …")).toBeInTheDocument();
  });

  it("shows the log tail once it has content, under any reason", async () => {
    vi.spyOn(api.heads, "log").mockResolvedValue("line one\nline two");
    renderFallback("no_transcript");
    expect(await screen.findByTestId("head-no-transcript-log")).toHaveTextContent("line one");
  });

  it("renders no log block at all while the fetch is loading — no permanent '…'", () => {
    vi.spyOn(api.heads, "log").mockReturnValue(new Promise(() => {})); // never resolves
    renderFallback("not_yet");
    expect(screen.queryByTestId("head-no-transcript-log")).not.toBeInTheDocument();
    expect(screen.queryByText("…")).not.toBeInTheDocument();
  });

  it("renders no log block when the log is truly empty — no duplicate heading as body", async () => {
    vi.spyOn(api.heads, "log").mockResolvedValue("   \n  ");
    renderFallback("no_reader");
    await screen.findByText("No live transcript for this harness");
    expect(screen.queryByTestId("head-no-transcript-log")).not.toBeInTheDocument();
  });

  it("renders no log block on a failed fetch — handled, not stuck on '…' forever", async () => {
    vi.spyOn(api.heads, "log").mockRejectedValue(new Error("API 500"));
    renderFallback("no_reader");
    await waitFor(() => expect(api.heads.log).toHaveBeenCalled());
    await waitFor(() => expect(screen.queryByTestId("head-no-transcript-log")).not.toBeInTheDocument());
    expect(screen.queryByText("…")).not.toBeInTheDocument();
  });
});
