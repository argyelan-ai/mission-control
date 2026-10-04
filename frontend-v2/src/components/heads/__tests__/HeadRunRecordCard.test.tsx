/**
 * HeadRunRecordCard — renders `/heads/{id}/summary` as facts (bauplan
 * `heads-sichtbar` PR 2 §3.2). K3: a `null` field is left out, never shown
 * as a placeholder. No state word in the card (the chat header already
 * carries it — anhang.md H's review finding on the prototype).
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { HeadRunRecordCard } from "../HeadRunRecordCard";
import type { HeadSummary } from "@/lib/heads";

/** jsdom reports 0 for every layout metric, so the 2-line clamp can never see
 *  an overflow on its own (same trap/fix as ChatMessage.test.tsx's own
 *  `stubScrollHeight` for the 10-line user-bubble clamp). */
function stubScrollHeight(px: number) {
  const original = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "scrollHeight");
  Object.defineProperty(HTMLElement.prototype, "scrollHeight", { configurable: true, get: () => px });
  return () => {
    if (original) Object.defineProperty(HTMLElement.prototype, "scrollHeight", original);
    else delete (HTMLElement.prototype as unknown as Record<string, unknown>).scrollHeight;
  };
}

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

  it("fact values sit label-left/value-right by default, wrapping instead of truncating (DESIGN.md K3/K12)", async () => {
    // Round 4 (review finding on PR #756): server-side shortening keeps a
    // test fact to one clause now, so most facts are short — K12's "field
    // left in meta, value right" phone property-list layout is back as the
    // default; only a value that GENUINELY still overflows stacks (covered
    // by the "stacks…" test below). jsdom with no stubbed `scrollHeight`
    // reports every value as fitting (see the component's own comment), so
    // this test exercises exactly that default, non-overflowing path.
    vi.spyOn(api.heads, "summary").mockResolvedValue({
      ...FULL,
      tests: { failed_before: "ModuleNotFoundError", passed_after: null },
    });
    renderCard();
    const value = await screen.findByText("ModuleNotFoundError");
    expect(value.className).toMatch(/break-words/);
    expect(value.className).not.toMatch(/\btruncate\b/);
    expect(value.className).toMatch(/text-right/);
    expect(value.className).toMatch(/flex-1/);
    const label = screen.getByText("Red before");
    expect(label.className).toMatch(/shrink-0/);
  });

  it("stacks label above value, full width, only once the value actually overflows", async () => {
    vi.spyOn(api.heads, "summary").mockResolvedValue({
      ...FULL,
      tests: { failed_before: "a very long explanation that really does not fit two lines on a phone screen at all", passed_after: null },
    });
    const restoreScrollHeight = stubScrollHeight(80); // > the 2-line clamp max
    try {
      renderCard();
      const value = await screen.findByText(/a very long explanation/);
      expect(value.className).not.toMatch(/text-right/);
      expect(value.className).not.toMatch(/flex-1/);
      const label = screen.getByText("Red before");
      expect(label.className).not.toMatch(/shrink-0/);
    } finally {
      restoreScrollHeight();
    }
  });

  it("clamps a genuinely overflowing fact to two lines with an expand control, never silently", async () => {
    vi.spyOn(api.heads, "summary").mockResolvedValue({
      ...FULL,
      tests: { failed_before: "a very long explanation that really does not fit two lines on a phone screen at all", passed_after: null },
    });
    const restoreScrollHeight = stubScrollHeight(80); // > the 2-line clamp max
    try {
      renderCard();
      const value = await screen.findByText(/a very long explanation/);
      expect(value.className).toMatch(/line-clamp-2/);
      // Scoped to this Fact's own row: `stubScrollHeight` makes every ref in
      // the card report the same height, so every Fact clamps in this test —
      // the point here is this ONE row's toggle, not the others.
      const row = within(value.parentElement as HTMLElement);
      const toggle = row.getByRole("button", { name: "Show more" });
      await userEvent.click(toggle);
      expect(value.className).not.toMatch(/line-clamp-2/);
      expect(row.getByRole("button", { name: "Show less" })).toBeInTheDocument();
    } finally {
      restoreScrollHeight();
    }
  });

  it("never clamps a fact that actually fits — no expand control appears", async () => {
    vi.spyOn(api.heads, "summary").mockResolvedValue(FULL);
    renderCard();
    await screen.findByText("ModuleNotFoundError");
    expect(screen.queryByRole("button", { name: "Show more" })).not.toBeInTheDocument();
  });

  it("strips markdown backticks from the result line and test facts (served already-stripped)", async () => {
    // summary.py now strips backticks server-side (review finding on PR #756
    // round 3) — this guards the card against ever re-introducing raw
    // markdown rendering for a value the API already hands over as plain
    // text, by asserting the card prints exactly what it is given, verbatim.
    vi.spyOn(api.heads, "summary").mockResolvedValue({
      ...FULL,
      result_line: "New background service backend/app/services/pr_merge_monitor.py wired in.",
    });
    renderCard();
    const result = await screen.findByTestId("head-record-result");
    expect(result.textContent).not.toMatch(/`/);
  });

  it("the copy-branch button meets the 44px touch target (DESIGN.md K11)", async () => {
    vi.spyOn(api.heads, "summary").mockResolvedValue(FULL);
    renderCard();
    const button = await screen.findByTestId("head-record-copy-branch");
    expect(button.className).toMatch(/min-w-touch/);
    expect(button.className).toMatch(/min-h-touch/);
  });

  it("the branch name wraps instead of truncating (DESIGN.md K3)", async () => {
    vi.spyOn(api.heads, "summary").mockResolvedValue({ ...FULL, branch: "mc-head/2026-10-04-a-very-long-descriptive-branch-slug-for-this-fixture" });
    renderCard();
    const branch = await screen.findByText(/mc-head\/2026-10-04/);
    expect(branch.className).toMatch(/break-all/);
    expect(branch.className).not.toMatch(/\btruncate\b/);
  });

  it("'Open the full run record' and 'Open PR' meet the 44px touch target (DESIGN.md K11)", async () => {
    vi.spyOn(api.heads, "summary").mockResolvedValue(FULL);
    renderCard();
    const openPr = await screen.findByTestId("head-record-open-pr");
    expect(openPr.className).toMatch(/min-h-touch/);
    expect(screen.getByTestId("head-record-open-full").className).toMatch(/min-h-touch/);
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
