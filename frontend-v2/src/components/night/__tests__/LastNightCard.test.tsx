/**
 * Home → "Last night" card (ROADMAP E2): MC is the operator's channel for the
 * morning report and blocked notices; Slack / Telegram are optional copies.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { api } from "@/lib/api";
import { mkRun } from "@/lib/__tests__/headFixtures";
import type { LastNight, LastNightEntry } from "@/lib/nightShift";
import { hasLastNight, lastNightRows } from "@/lib/nightShift";
import { LastNightCard, LastNightCardView } from "../LastNightCard";

function wrap(ui: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

function row(over: Partial<LastNightEntry> = {}): LastNightEntry {
  return {
    task_id: "t-1",
    title: "Fix flaky retry test",
    started: true,
    state: "passed",
    reason: null,
    blocked: null,
    pr_url: "https://github.com/acme/tool/pull/9",
    category: "passed",
    run: null,
    ...over,
  };
}

const asking = row({
  task_id: "t-2",
  title: "Remove old endpoint",
  state: "needs_you",
  category: "needs_you",
  pr_url: null,
  run: mkRun({ run_id: "22222222-2222-4222-8222-222222222222", state: "needs_you", question: "Two callers still use it. Remove anyway?" }),
});

const report = (entries: LastNightEntry[]): LastNight => ({
  report: { night: "2026-09-23", sent_at: "2026-09-24T04:01:00Z", delivered: false, dismissed: false, entries },
  notices: [],
});

const full = report([
  row(),
  asking,
  row({ task_id: "t-3", title: "Upload worker", state: "failed", reason: "exit_1", category: "failed", pr_url: null }),
  row({ task_id: "t-4", title: "Docs pass", started: false, state: null, reason: "engine_not_ready", category: "blocked", pr_url: null }),
]);

beforeEach(() => {
  vi.restoreAllMocks();
});

describe("Last night card", () => {
  it("renders every state with counts, one line per job linking to the task", () => {
    wrap(<LastNightCardView data={full} />);
    const card = screen.getByTestId("last-night-card");
    expect(card).toHaveTextContent("Last night");
    const counts = screen.getByTestId("last-night-counts");
    expect(counts).toHaveTextContent("1 passed");
    expect(counts).toHaveTextContent("1 failed");
    expect(counts).toHaveTextContent("1 needs you");
    expect(counts).toHaveTextContent("1 blocked");

    const rows = screen.getAllByTestId(/^last-night-row-/);
    // needs you first, what went fine last
    expect(rows.map((r) => r.getAttribute("data-testid"))).toEqual([
      "last-night-row-t-2",
      "last-night-row-t-4",
      "last-night-row-t-3",
      "last-night-row-t-1",
    ]);
    for (const r of rows) {
      const id = r.getAttribute("data-testid")!.replace("last-night-row-", "");
      expect(within(r).getByRole("link", { name: /Open task/ })).toHaveAttribute("href", `/tasks?task=${id}`);
    }
    expect(rows[0]).toHaveTextContent("Two callers still use it. Remove anyway?");
    expect(rows[1]).toHaveTextContent("not started · the model is not running");
    expect(rows[2]).toHaveTextContent("failed");
    expect(within(rows[3]).getByRole("link", { name: "PR #9" })).toHaveAttribute("href", "https://github.com/acme/tool/pull/9");
    // only the needs-you line has an Answer action
    expect(screen.getAllByRole("button", { name: /^Answer/ })).toHaveLength(1);
    expect(within(rows[0]).getByRole("button", { name: /^Answer/ })).toBeInTheDocument();
  });

  it("answers a head that needs you: restart on the same pair with the answer", async () => {
    const restart = vi.spyOn(api.heads, "restart").mockResolvedValue({ run_id: "new", state: "starting", restarted_from: asking.run!.run_id });
    wrap(<LastNightCardView data={full} />);
    const r = screen.getByTestId("last-night-row-t-2");
    await userEvent.click(within(r).getByRole("button", { name: /^Answer/ }));
    const box = within(r).getByLabelText("Your answer");
    const send = within(r).getByRole("button", { name: "Answer & continue" });
    expect(send).toBeDisabled();
    await userEvent.type(box, "Yes, remove it");
    await userEvent.click(send);
    await waitFor(() => expect(restart).toHaveBeenCalledTimes(1));
    expect(restart).toHaveBeenCalledWith(asking.run!.run_id, {
      harness: "omp",
      runtime_slug: "glm-local",
      mode: "continue",
      answer: "Yes, remove it",
    });
  });

  it("offers no Answer when the question was answered meanwhile", () => {
    const answered = { ...asking, run: mkRun({ state: "running", question: null }) };
    wrap(<LastNightCardView data={report([answered])} />);
    expect(screen.queryByRole("button", { name: /^Answer/ })).toBeNull();
    expect(screen.getByTestId("last-night-row-t-2")).toHaveTextContent("answered or restarted since");
  });

  it("dismiss hides it", async () => {
    const dismiss = vi.spyOn(api.nightShift, "dismissLastNight").mockResolvedValue({ night: "2026-09-23", dismissed: true });
    wrap(<LastNightCardView data={full} />);
    await userEvent.click(screen.getByRole("button", { name: "Dismiss last night's report" }));
    expect(dismiss).toHaveBeenCalledWith("2026-09-23");
    await waitFor(() => expect(screen.queryByTestId("last-night-card")).toBeNull());
  });

  it("shows blocked notices during the night, with Answer for a question", () => {
    wrap(
      <LastNightCardView
        data={{
          report: null,
          notices: [
            { task_id: "t-5", title: "Silent job", kind: "silent", silent_s: 22 * 60, run: mkRun({ state: "running" }) },
            { task_id: "t-6", title: "Asking job", kind: "needs_you", silent_s: null, run: mkRun({ state: "needs_you", question: "Which one?" }) },
          ],
        }}
      />,
    );
    expect(screen.getByTestId("last-night-card")).toHaveTextContent("Night shift · now");
    expect(screen.getByTestId("last-night-notice-t-5")).toHaveTextContent("silent for 22 min");
    const q = screen.getByTestId("last-night-notice-t-6");
    expect(q).toHaveTextContent("Which one?");
    expect(within(q).getByRole("button", { name: /^Answer/ })).toBeInTheDocument();
    // nothing to dismiss while it is live
    expect(screen.queryByRole("button", { name: "Dismiss last night's report" })).toBeNull();
  });

  it("is hidden when nothing ran", async () => {
    expect(hasLastNight({ report: null, notices: [] })).toBe(false);
    expect(hasLastNight(report([]))).toBe(false);
    expect(hasLastNight(full)).toBe(true);
    const { container } = wrap(<LastNightCardView data={{ report: null, notices: [] }} />);
    expect(container).toBeEmptyDOMElement();

    vi.spyOn(api.heads, "occupancy").mockResolvedValue({ boxes: {} });
    const get = vi.spyOn(api.nightShift, "lastNight").mockResolvedValue({ report: null, notices: [] });
    const other = wrap(<LastNightCard />);
    await waitFor(() => expect(get).toHaveBeenCalled());
    expect(other.container).toBeEmptyDOMElement();
  });

  it("is hidden while heads are off (no request)", async () => {
    vi.spyOn(api.heads, "occupancy").mockRejectedValue(new Error('API 404: {"detail":{"code":"heads_disabled"}}'));
    const get = vi.spyOn(api.nightShift, "lastNight").mockResolvedValue(full);
    const { container } = wrap(<LastNightCard />);
    await new Promise((r) => setTimeout(r, 30));
    expect(get).not.toHaveBeenCalled();
    expect(container).toBeEmptyDOMElement();
  });

  it("orders rows needs-you first and keeps the order inside a category", () => {
    const rows = lastNightRows({
      entries: [row({ task_id: "a" }), row({ task_id: "b", category: "needs_you" }), row({ task_id: "c" })],
    });
    expect(rows.map((r) => r.task_id)).toEqual(["b", "a", "c"]);
  });
});
