import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { ScheduledJob } from "@/lib/types";

// The job header used to carry an "ON"/"OFF" chip that was really a button:
// one click switched a recurring job off with no label, prompt or undo.

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  usePathname: () => "/schedule/job-1",
  useParams: () => ({ jobId: "job-1" }),
  useSearchParams: () => new URLSearchParams(),
}));
vi.mock("@/components/layout/AppShell", () => ({
  default: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));
vi.mock("recharts", () => {
  const Stub = ({ children }: { children?: React.ReactNode }) => <div>{children}</div>;
  return {
    AreaChart: Stub, Area: Stub, XAxis: Stub, YAxis: Stub, CartesianGrid: Stub, Tooltip: Stub,
    ResponsiveContainer: Stub,
  };
});
vi.mock("@/lib/store", () => ({
  useAppStore: (selector?: (s: { activeBoardId: null }) => unknown) =>
    selector ? selector({ activeBoardId: null }) : { activeBoardId: null },
}));

import JobDetailPage from "../page";

const JOB = {
  id: "job-1",
  name: "Morning digest",
  enabled: true,
  schedule_type: "daily",
  schedule_time: "07:00",
  next_run_at: "2030-01-02T07:00:00Z",
  task_board_id: null,
} as unknown as ScheduledJob;

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <JobDetailPage />
    </QueryClientProvider>
  );
}

let update: ReturnType<typeof vi.fn>;
function setup(job: ScheduledJob = JOB) {
  vi.spyOn(api.schedule, "listJobs").mockResolvedValue([job]);
  vi.spyOn(api.schedule, "getStats").mockResolvedValue({} as never);
  vi.spyOn(api.schedule, "getRuns").mockResolvedValue([]);
  vi.spyOn(api.schedule, "getCreatedTasks").mockResolvedValue([]);
  vi.spyOn(api.agents, "list").mockResolvedValue([]);
  vi.spyOn(api.boards, "list").mockResolvedValue([]);
  update = vi.spyOn(api.schedule, "updateJob").mockResolvedValue(job) as unknown as ReturnType<typeof vi.fn>;
}

describe("Job detail — Enabled switch", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("is a labelled switch, not an ON/OFF chip", async () => {
    setup();
    renderPage();
    const sw = await screen.findByRole("switch", { name: "Enabled" });
    expect(sw).toHaveAttribute("aria-checked", "true");
    expect(screen.queryByText("ON")).not.toBeInTheDocument();
  });

  it("switching off asks first and names what will not run", async () => {
    setup();
    renderPage();
    await userEvent.click(await screen.findByRole("switch", { name: "Enabled" }));
    expect(update).not.toHaveBeenCalled();
    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveTextContent("Morning digest");
    expect(dialog).toHaveTextContent(/will not run/);
  });

  it("confirming switches the job off", async () => {
    setup();
    renderPage();
    await userEvent.click(await screen.findByRole("switch", { name: "Enabled" }));
    await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Disable job" }));
    expect(update).toHaveBeenCalledWith("job-1", { enabled: false });
  });

  it("switching back on needs no confirmation", async () => {
    setup({ ...JOB, enabled: false } as ScheduledJob);
    renderPage();
    const sw = await screen.findByRole("switch", { name: "Enabled" });
    expect(sw).toHaveAttribute("aria-checked", "false");
    await userEvent.click(sw);
    expect(update).toHaveBeenCalledWith("job-1", { enabled: true });
  });
});
