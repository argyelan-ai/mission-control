/**
 * Round-3 user test t4 S8: paused jobs read "Next: 24d ago", "Success rate
 * 0 %" without a run, an English "success", ISO dates on the chart axis, and
 * on the phone the table was cut off on the right.
 */
import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { JobsTable } from "../JobsTable";
import { formatChartDay, nextRunRelative, runStatusLabel } from "../jobDisplay";
import { api } from "@/lib/api";
import type { ScheduledJob } from "@/lib/types";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  usePathname: () => "/schedule/job-1",
  useParams: () => ({ jobId: "job-1" }),
  useSearchParams: () => new URLSearchParams(),
}));
vi.mock("@/components/layout/AppShell", () => ({
  default: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));
vi.mock("recharts", () => {
  const Stub = ({ children }: { children?: React.ReactNode }) => <div>{children}</div>;
  return { AreaChart: Stub, Area: Stub, XAxis: Stub, YAxis: Stub, CartesianGrid: Stub, Tooltip: Stub, ResponsiveContainer: Stub };
});
vi.mock("@/lib/store", () => ({
  useAppStore: (selector?: (s: { activeBoardId: null }) => unknown) =>
    selector ? selector({ activeBoardId: null }) : { activeBoardId: null },
}));

import JobDetailPage from "@/app/schedule/[jobId]/page";

const DAY = 86_400_000;
const PAUSED = {
  id: "job-1",
  name: "Morning Briefing for the whole team",
  description: null,
  enabled: false,
  schedule_type: "daily",
  schedule_time: "07:00",
  schedule_interval_hours: null,
  action_type: "create_task",
  agent_id: null,
  agent_name: "alpha",
  message: "hi",
  retry_max: 0,
  retry_delay_minutes: 0,
  task_board_id: null,
  task_skip_review: false,
  last_run_at: new Date(Date.now() - 25 * DAY).toISOString(),
  last_run_status: "success",
  last_run_error: null,
  // The time it had when it was switched off — weeks in the past.
  next_run_at: new Date(Date.now() - 24 * DAY).toISOString(),
  created_at: "2026-09-01T00:00:00Z",
} as unknown as ScheduledJob;

const noop = () => {};
const t = (k: string, v?: Record<string, string | number>) => (v ? `${k}:${JSON.stringify(v)}` : k);

describe("jobDisplay", () => {
  it("a paused job has no next run", () => {
    expect(nextRunRelative(PAUSED, t)).toBe("nextPaused");
    expect(nextRunRelative({ ...PAUSED, enabled: true }, t)).toMatch(/^dAgo/);
  });
  it("names a run status in the UI language, keeps an unknown one", () => {
    expect(runStatusLabel("success", t)).toBe("runStatus.success");
    expect(runStatusLabel("weird", t)).toBe("weird");
    expect(runStatusLabel(null, t)).toBe("—");
  });
  it("puts chart days in the local date form", () => {
    expect(formatChartDay("2026-09-23", "de-CH")).toBe("23.09.");
    expect(formatChartDay("2026-09-23", "en-GB")).toBe("23/09");
    expect(formatChartDay("not a day", "de-CH")).toBe("not a day");
  });
});

describe("JobsTable — paused job", () => {
  it("says '— (paused)' instead of a next run in the past", () => {
    render(<JobsTable jobs={[PAUSED]} onEdit={noop} onDelete={noop} onTrigger={noop} onToggleEnabled={noop} onSnooze={noop} onDuplicate={noop} />);
    const table = screen.getByRole("region", { name: "Jobs table" });
    expect(within(table).getByText("— (paused)")).toBeInTheDocument();
    expect(within(table).queryByText("24d ago")).not.toBeInTheDocument();
  });

  it("on the phone a job is a card — whole name, next run, actions behind ⋯", async () => {
    const onTrigger = vi.fn();
    render(<JobsTable jobs={[PAUSED]} onEdit={noop} onDelete={noop} onTrigger={onTrigger} onToggleEnabled={noop} onSnooze={noop} onDuplicate={noop} />);
    const card = screen.getByTestId("job-card");
    // The card list shows only below md (one md:hidden, on the list's wrapper).
    expect(card.closest(".md\\:hidden")).not.toBeNull();
    expect(within(card).getByText("Morning Briefing for the whole team")).not.toHaveClass("truncate");
    expect(within(card).getByText(/— \(paused\)/)).toBeInTheDocument();
    // The table itself only from md up.
    expect(screen.getByRole("region", { name: "Jobs table" }).className).toMatch(/hidden md:block/);
    await userEvent.click(within(card).getByRole("button", { name: "Actions for Morning Briefing for the whole team" }));
    await userEvent.click(screen.getByRole("menuitem", { name: "Run now" }));
    expect(onTrigger).toHaveBeenCalledWith("job-1");
  });
});

describe("Job detail — paused job", () => {
  beforeEach(() => vi.restoreAllMocks());

  function renderPage(stats: Record<string, unknown>) {
    vi.spyOn(api.schedule, "listJobs").mockResolvedValue([PAUSED]);
    vi.spyOn(api.schedule, "getStats").mockResolvedValue(stats as never);
    vi.spyOn(api.schedule, "getRuns").mockResolvedValue([]);
    vi.spyOn(api.schedule, "getCreatedTasks").mockResolvedValue([]);
    vi.spyOn(api.agents, "list").mockResolvedValue([]);
    vi.spyOn(api.boards, "list").mockResolvedValue([]);
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    return render(
      <QueryClientProvider client={qc}>
        <JobDetailPage />
      </QueryClientProvider>,
    );
  }

  it("next run '— (paused)', no rate without runs, the status in words", async () => {
    renderPage({ success_rate_7d: null, success_rate_30d: 1, avg_duration_ms: 24, p95_duration_ms: 24, total_runs_30d: 5, runs_by_day: [] });
    const next = await screen.findByText("Next run");
    expect(next.parentElement).toHaveTextContent("— (paused)");
    const rate = screen.getByText("Success Rate (7d)");
    expect(rate.closest("[data-testid='kpi-card']") ?? rate.parentElement?.parentElement).not.toHaveTextContent("0%");
    const last = screen.getByText("Last status");
    expect(last.parentElement).toHaveTextContent("Succeeded");
    expect(last.parentElement).not.toHaveTextContent("success");
  });
});
