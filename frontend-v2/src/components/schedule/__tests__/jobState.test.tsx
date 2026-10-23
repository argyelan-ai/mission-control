/**
 * Round-4 (r2 S3, t4 NICE-4, r2 N5): the head of a paused job read "Enabled"
 * next to a switch set to off — the state itself stood deep in the config
 * card. "paused" and "disable" were two words for one state, and in German
 * "Pausieren" named both switching a job off and snoozing it for N hours.
 * The config said "create_task" raw, the desktop list "26d ago", durations
 * "23ms".
 */
import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { JobsTable } from "../JobsTable";
import { actionTypeLabel, formatDuration } from "../jobDisplay";
import { api } from "@/lib/api";
import type { ScheduledJob } from "@/lib/types";
import de from "../../../../messages/de.json";

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
  name: "Morning Briefing",
  enabled: false,
  schedule_type: "daily",
  schedule_time: "07:00",
  action_type: "create_task",
  agent_name: null,
  retry_max: 0,
  retry_delay_minutes: 0,
  task_board_id: null,
  task_skip_review: false,
  last_run_at: new Date(Date.now() - 26 * DAY).toISOString(),
  last_run_status: "success",
  next_run_at: new Date(Date.now() - 25 * DAY).toISOString(),
  snoozed_until: null,
} as unknown as ScheduledJob;

function renderDetail(job: ScheduledJob) {
  vi.spyOn(api.schedule, "listJobs").mockResolvedValue([job]);
  vi.spyOn(api.schedule, "getStats").mockResolvedValue({ avg_duration_ms: 23, p95_duration_ms: 1234, total_runs_30d: 4, runs_by_day: [] } as never);
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

describe("Job detail — one state sentence under the title", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("a paused job says 'Paused' in its head, with the last run", async () => {
    renderDetail(PAUSED);
    const state = await screen.findByTestId("job-state");
    expect(state).toHaveTextContent(/^Paused · last run 26 days ago$/);
    // The switch is a setting, not a state word: no "Enabled" beside "off".
    const sw = screen.getByRole("switch", { name: "Run on schedule" });
    expect(sw).toHaveAttribute("aria-checked", "false");
    expect(screen.queryByText("Enabled")).not.toBeInTheDocument();
  });

  it("an active job names its next run; a snoozed one until when", async () => {
    const { unmount } = renderDetail({
      ...PAUSED,
      enabled: true,
      next_run_at: new Date(Date.now() + 3 * 3_600_000 + 60_000).toISOString(),
    } as ScheduledJob);
    expect(await screen.findByTestId("job-state")).toHaveTextContent(/^Active · next run in about 3 hours$/);
    unmount();
    vi.restoreAllMocks();
    renderDetail({ ...PAUSED, enabled: true, snoozed_until: "2030-01-02T14:00:00Z" } as ScheduledJob);
    expect(await screen.findByTestId("job-state")).toHaveTextContent(/^Snoozed until /);
  });

  it("switching off is 'Pause', the same word the list uses", async () => {
    renderDetail({ ...PAUSED, enabled: true, next_run_at: "2030-01-02T07:00:00Z" } as ScheduledJob);
    await userEvent.click(await screen.findByRole("switch", { name: "Run on schedule" }));
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByRole("button", { name: "Pause job" })).toBeInTheDocument();
    expect(dialog).not.toHaveTextContent(/disable/i);
  });

  it("the action in words, durations with a space", async () => {
    renderDetail(PAUSED);
    const action = await screen.findByText("Action");
    expect(action.parentElement).toHaveTextContent("Create task");
    expect(action.parentElement).not.toHaveTextContent("create_task");
    expect(screen.getByText("23 ms")).toBeInTheDocument();
    expect(screen.getByText("1.2 s")).toBeInTheDocument();
  });
});

describe("jobDisplay — words and forms", () => {
  const t = (k: string) => k;
  it("names the action types, keeps an unknown one", () => {
    expect(actionTypeLabel("create_task", t)).toBe("actionType.create_task");
    expect(actionTypeLabel("chat_send", t)).toBe("actionType.chat_send");
    expect(actionTypeLabel("weird", t)).toBe("weird");
  });
  it("durations in the UI's number form with a space", () => {
    expect(formatDuration(23, "en")).toBe("23 ms");
    expect(formatDuration(1234, "de")).toBe("1,2 s");
    expect(formatDuration(90_000, "de")).toBe("1,5 min");
    expect(formatDuration(0, "de")).toBe("—");
  });
  it("German: switching off and snoozing are two different words", () => {
    const s = de.schedule as unknown as Record<string, string>;
    expect(s.snooze).not.toBe(s.pause);
    expect(s.snoozedUntilLabel).not.toMatch(/^Pausiert/);
    expect(s.disableJobConfirm).toMatch(/pausieren/i);
  });
});

describe("Jobs list (desktop) — last run in words", () => {
  it("reads '26 days ago', not '26d ago'", () => {
    const noop = () => {};
    render(<JobsTable jobs={[PAUSED]} onEdit={noop} onDelete={noop} onTrigger={noop} onToggleEnabled={noop} onSnooze={noop} onDuplicate={noop} />);
    const table = screen.getByRole("region", { name: "Jobs table" });
    expect(within(table).getByText("26 days ago")).toBeInTheDocument();
    expect(within(table).queryByText("26d ago")).not.toBeInTheDocument();
  });
});
