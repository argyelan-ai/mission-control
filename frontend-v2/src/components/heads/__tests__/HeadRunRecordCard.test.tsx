/**
 * HeadRunRecordCard — renders `/heads/{id}/summary` as facts (bauplan
 * `heads-sichtbar` PR 2 §3.2). K3: a `null` field is left out, never shown
 * as a placeholder. No state word in the card (the chat header already
 * carries it — anhang.md H's review finding on the prototype).
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { HeadRunRecordCard } from "../HeadRunRecordCard";
import type { HeadSummary } from "@/lib/heads";

function renderCard() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <HeadRunRecordCard runId="run-1" />
    </QueryClientProvider>,
  );
}

const FULL: HeadSummary = {
  run_id: "run-1",
  status: "passed",
  result_line: "Added the summary card.",
  tests: { failed_before: "ModuleNotFoundError", passed_after: "9 passed" },
  sabotage: true,
  kz_ok: true,
  review: "helper",
  bypass: 0,
  operator_minutes: 12,
  helpers: 2,
  branch: "mc-head/summary-card-1234",
  pr_url: "https://github.com/o/r/pull/9",
};

const MOSTLY_NULL: HeadSummary = {
  run_id: "run-1",
  status: "passed",
  result_line: "Fixture run record.",
  tests: { failed_before: null, passed_after: null },
  sabotage: null,
  kz_ok: null,
  review: null,
  bypass: null,
  operator_minutes: null,
  helpers: null,
  branch: null,
  pr_url: null,
};

beforeEach(() => vi.restoreAllMocks());

describe("HeadRunRecordCard", () => {
  it("renders every fact when the record has every field", async () => {
    vi.spyOn(api.heads, "summary").mockResolvedValue(FULL);
    renderCard();
    await waitFor(() => expect(screen.getByTestId("head-record-result")).toHaveTextContent("Added the summary card."));
    expect(screen.getByText("ModuleNotFoundError")).toBeInTheDocument();
    expect(screen.getByText("9 passed")).toBeInTheDocument();
    expect(screen.getByText("mc-head/summary-card-1234")).toBeInTheDocument();
    expect(screen.getByTestId("head-record-open-pr")).toHaveTextContent("PR #9");
    // No state word anywhere in the card (distinct from "9 passed", the
    // test-count fact, which legitimately contains the substring).
    expect(screen.queryByText(/^Passed$/)).not.toBeInTheDocument();
  });

  it("never shows 'null'/'undefined'/a placeholder dash for a missing field — it is just absent", async () => {
    vi.spyOn(api.heads, "summary").mockResolvedValue(MOSTLY_NULL);
    renderCard();
    await waitFor(() => expect(screen.getByTestId("head-record-result")).toHaveTextContent("Fixture run record."));
    const card = screen.getByTestId("head-run-record-card");
    expect(card.textContent).not.toMatch(/null|undefined|NaN/i);
    expect(screen.queryByTestId("head-record-copy-branch")).not.toBeInTheDocument();
    expect(screen.queryByTestId("head-record-open-pr")).not.toBeInTheDocument();
  });

  it("renders nothing at all when there is no run record (404)", async () => {
    vi.spyOn(api.heads, "summary").mockRejectedValue(new Error("API 404: {}"));
    renderCard();
    await waitFor(() => expect(api.heads.summary).toHaveBeenCalled());
    expect(screen.queryByTestId("head-run-record-card")).not.toBeInTheDocument();
  });

  it("fact values wrap instead of truncating, and the label never does (DESIGN.md K3)", async () => {
    vi.spyOn(api.heads, "summary").mockResolvedValue({
      ...FULL,
      tests: { failed_before: "ModuleNotFoundError: app.services.heads.transcript — no module named transcript_adapters", passed_after: null },
    });
    renderCard();
    const value = await screen.findByText(/ModuleNotFoundError/);
    expect(value.className).toMatch(/break-words/);
    expect(value.className).not.toMatch(/\btruncate\b/);
    const label = screen.getByText("Red before");
    expect(label.className).toMatch(/whitespace-nowrap/);
    expect(label.className).toMatch(/shrink-0/);
  });

  it("the copy-branch button meets the 44px touch target (DESIGN.md K11)", async () => {
    vi.spyOn(api.heads, "summary").mockResolvedValue(FULL);
    renderCard();
    const button = await screen.findByTestId("head-record-copy-branch");
    expect(button.className).toMatch(/min-w-touch/);
    expect(button.className).toMatch(/min-h-touch/);
  });

  it("'Open the full run record' lazily fetches the raw markdown", async () => {
    vi.spyOn(api.heads, "summary").mockResolvedValue(FULL);
    const runRecord = vi.spyOn(api.heads, "runRecord").mockResolvedValue("# Run record\n\nfull text");
    renderCard();
    await waitFor(() => expect(screen.getByTestId("head-record-result")).toBeInTheDocument());
    expect(runRecord).not.toHaveBeenCalled();
    await userEvent.click(screen.getByTestId("head-record-open-full"));
    await waitFor(() => expect(screen.getByTestId("head-record-full")).toHaveTextContent("full text"));
  });
});
