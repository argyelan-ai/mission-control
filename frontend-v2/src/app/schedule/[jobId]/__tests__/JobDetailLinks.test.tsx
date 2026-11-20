import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { ScheduledJob } from "@/lib/types";

// Etappe 2 (PR A): the detail page linked runs and created tasks to
// `/tasks/<id>` — a route that never existed (404). Now they open the chat of
// the agent that did the work, or show no link when there is no agent.

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

const RUNS = [
  { id: "r1", job_id: "job-1", started_at: "2026-09-27T07:00:00Z", finished_at: "2026-09-27T07:01:00Z", status: "success", error: null, detail: null, retry_attempt: 0, task_id: "t1" },
  { id: "r2", job_id: "job-1", started_at: "2026-09-26T07:00:00Z", finished_at: "2026-09-26T07:01:00Z", status: "success", error: null, detail: null, retry_attempt: 0, task_id: "t-gone" },
];
const TASKS = [
  { id: "t1", title: "Digest for Monday", status: "done", assigned_agent_id: "a-task", completed_at: "2026-09-27T07:01:00Z", started_at: null },
  { id: "t2", title: "Digest nobody ran", status: "done", assigned_agent_id: null, completed_at: null, started_at: null },
];

function setup(job: ScheduledJob, chatAgents: string[] = ["a-task", "a-job"]) {
  // The chat list: only these agents have a chat the link can open.
  vi.spyOn(api.agents, "listDockerSessions").mockResolvedValue(chatAgents.map((id) => ({ id })) as never);
  vi.spyOn(api.agents, "listHostSessions").mockResolvedValue([] as never);
  vi.spyOn(api.schedule, "listJobs").mockResolvedValue([job]);
  vi.spyOn(api.schedule, "getStats").mockResolvedValue({} as never);
  vi.spyOn(api.schedule, "getRuns").mockResolvedValue(RUNS as never);
  vi.spyOn(api.schedule, "getCreatedTasks").mockResolvedValue(TASKS as never);
  vi.spyOn(api.agents, "list").mockResolvedValue([{ id: "a-task", name: "Writer" }] as never);
  vi.spyOn(api.boards, "list").mockResolvedValue([]);
}

function allHrefs(): string[] {
  return screen.queryAllByRole("link").map((l) => l.getAttribute("href") ?? "");
}

describe("Job detail — links go to chats, never to /tasks/<id>", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("a created task opens the chat of its agent; without an agent it is plain text", async () => {
    setup(JOB);
    renderPage();
    await screen.findByText("Digest for Monday");
    await waitFor(() => expect(screen.getByText("Digest for Monday").closest("a")).not.toBeNull());
    const row = screen.getByText("Digest for Monday").closest("a");
    expect(row).toHaveAttribute("href", "/sessions?agent=a-task&enter=1");
    expect(screen.getByText("Digest nobody ran").closest("a")).toBeNull();
    expect(allHrefs().filter((h) => h.startsWith("/tasks"))).toEqual([]);
  });

  it("a run opens the chat of its task's agent, else of the job's agent", async () => {
    setup({ ...JOB, agent_id: "a-job" } as ScheduledJob);
    renderPage();
    await screen.findByText("Digest for Monday");
    await waitFor(() => expect(screen.getAllByRole("link", { name: "Open chat" })).toHaveLength(2));
    const runLinks = screen.getAllByRole("link", { name: "Open chat" }).map((l) => l.getAttribute("href"));
    expect(runLinks).toEqual(["/sessions?agent=a-task&enter=1", "/sessions?agent=a-job&enter=1"]);
    expect(allHrefs().filter((h) => h.startsWith("/tasks"))).toEqual([]);
  });

  it("a run with no agent anywhere shows no link", async () => {
    setup(JOB);
    renderPage();
    await screen.findByText("Digest for Monday");
    // r1 still reaches its task's agent; r2 (task not listed, job has no agent) has none.
    await waitFor(() => expect(screen.getAllByRole("link", { name: "Open chat" })).toHaveLength(1));
  });

  it("an agent without a chat (archived) gets no link — the chat page would open another chat", async () => {
    setup({ ...JOB, agent_id: "a-job" } as ScheduledJob, ["a-job"]);
    renderPage();
    await screen.findByText("Digest for Monday");
    // a-task is archived: its task row is plain; the run falls back to the job's agent.
    await waitFor(() => expect(screen.getAllByRole("link", { name: "Open chat" })).toHaveLength(2));
    expect(screen.getByText("Digest for Monday").closest("a")).toBeNull();
    const runLinks = screen.getAllByRole("link", { name: "Open chat" }).map((l) => l.getAttribute("href"));
    expect(runLinks).toEqual(["/sessions?agent=a-job&enter=1", "/sessions?agent=a-job&enter=1"]);
    expect(allHrefs().filter((h) => h.includes("a-task"))).toEqual([]);
  });

  it("nothing links while the chat list has not loaded or no agent has a chat", async () => {
    setup({ ...JOB, agent_id: "a-job" } as ScheduledJob, []);
    renderPage();
    await screen.findByText("Digest for Monday");
    await waitFor(() => expect(api.agents.listDockerSessions).toHaveBeenCalled());
    expect(allHrefs().filter((h) => h.startsWith("/sessions"))).toEqual([]);
  });
});
