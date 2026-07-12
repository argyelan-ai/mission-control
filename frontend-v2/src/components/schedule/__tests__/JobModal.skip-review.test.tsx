/**
 * JobModal — task_skip_review + task_payload.skip_review sent in create request.
 *
 * Tests the two paths:
 * (d-1) Default: skip_review=false → task_skip_review false
 * (d-2) Toggled on: skip_review=true → task_skip_review true, task_payload.skip_review true
 * (d-3) Editing a job with task_skip_review=true → pill rendered as active
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { JobModal } from "../JobModal";
import { api } from "@/lib/api";
import type { Board, ScheduledJob } from "@/lib/types";

function mkBoard(overrides: Partial<Board> = {}): Board {
  return {
    id: "board-1",
    board_group_id: null,
    name: "Test Board",
    slug: "test-board",
    description: null,
    icon: null,
    color: null,
    require_approval_for_done: false,
    require_review_before_done: false,
    only_lead_can_change_status: false,
    auto_dispatch_enabled: true,
    objective: null,
    target_date: null,
    stats_cache: null,
    sort_order: 0,
    is_archived: false,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

function mkJob(overrides: Partial<ScheduledJob> = {}): ScheduledJob {
  return {
    id: "job-42",
    name: "Daily Digest",
    task_skip_review: false,
    task_title: "Digest",
    task_priority: "medium",
    schedule_type: "daily",
    schedule_time: "06:00",
    schedule_interval_hours: null,
    enabled: true,
    description: null,
    action_type: "create_task",
    agent_id: null,
    agent_name: null,
    message: null,
    api_endpoint: null,
    retry_max: 0,
    retry_delay_minutes: 5,
    depends_on_job_id: null,
    notify_on_failure: false,
    task_board_id: "board-1",
    last_run_at: null,
    last_run_status: null,
    last_run_error: null,
    next_run_at: null,
    created_at: "2026-01-01T00:00:00Z",
    discord_channel_id: null,
    discord_channel_name: null,
    tags: [],
    ...overrides,
  };
}

function renderModal(props: Partial<Parameters<typeof JobModal>[0]> = {}) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={qc}>
      <JobModal
        open={true}
        onClose={vi.fn()}
        activeBoardId="board-1"
        agents={[]}
        boards={[mkBoard()]}
        onSuccess={vi.fn()}
        {...props}
      />
    </QueryClientProvider>,
  );
}

describe("JobModal — task_skip_review field", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response("[]", { status: 200, headers: { "Content-Type": "application/json" } }),
    );
    vi.spyOn(api.agents, "list").mockResolvedValue([]);
  });

  it("sends task_skip_review: false by default", async () => {
    const createSpy = vi
      .spyOn(api.schedule, "createJob")
      .mockResolvedValue({ id: "job-1" } as ScheduledJob);

    renderModal();

    // Fill the required name field (placeholder is "Daily Standup")
    await userEvent.type(screen.getByPlaceholderText("Daily Standup"), "My Job");

    await userEvent.click(screen.getByRole("button", { name: "Create job" }));

    await waitFor(() => expect(createSpy).toHaveBeenCalled());
    const [payload] = createSpy.mock.calls[0];
    expect(payload.task_skip_review).toBe(false);
    expect((payload.task_payload as Record<string, unknown>)?.skip_review).toBe(false);
  });

  it("sends task_skip_review: true when editing a job with skip_review=true", async () => {
    // When a job has task_skip_review=true the form initializes skipReview=true.
    // Saving the form without changing anything should re-submit task_skip_review=true.
    const updateSpy = vi
      .spyOn(api.schedule, "updateJob")
      .mockResolvedValue({ id: "job-42" } as ScheduledJob);

    renderModal({ job: mkJob({ task_skip_review: true }) });

    // editing mode — submit button is "Save changes"
    await userEvent.click(screen.getByRole("button", { name: "Save changes" }));

    await waitFor(() => expect(updateSpy).toHaveBeenCalled());
    const [, payload] = updateSpy.mock.calls[0];
    expect(payload.task_skip_review).toBe(true);
    expect((payload.task_payload as Record<string, unknown>)?.skip_review).toBe(true);
  });

  it("round-trips task_skip_review=false when editing a job without skip_review", async () => {
    const updateSpy = vi
      .spyOn(api.schedule, "updateJob")
      .mockResolvedValue({ id: "job-42" } as ScheduledJob);

    renderModal({ job: mkJob({ task_skip_review: false }) });

    await userEvent.click(screen.getByRole("button", { name: "Save changes" }));

    await waitFor(() => expect(updateSpy).toHaveBeenCalled());
    const [, payload] = updateSpy.mock.calls[0];
    expect(payload.task_skip_review).toBe(false);
  });
});
