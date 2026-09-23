/**
 * Schedule jobs table — header and rows must share one column grid.
 *
 * Header and each row are separate CSS grids with the same template. The last
 * (actions) column used to be `auto`: rows hold ~190 px of hover buttons
 * there, the header holds nothing, so every `fr` column ended up a different
 * width and the header labels drifted away from their values.
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { JobsTable } from "../JobsTable";
import type { ScheduledJob } from "@/lib/types";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));

const job: ScheduledJob = {
  id: "job-1",
  name: "Morning digest",
  description: null,
  enabled: true,
  schedule_type: "daily",
  schedule_time: "07:00",
  schedule_interval_hours: null,
  action_type: "chat_send",
  agent_id: null,
  agent_name: null,
  message: "hi",
  api_endpoint: null,
  retry_max: 0,
  retry_delay_minutes: 0,
  depends_on_job_id: null,
  notify_on_failure: false,
  task_board_id: null,
  task_title: null,
  task_priority: null,
  task_skip_review: false,
  last_run_at: null,
  last_run_status: null,
  last_run_error: null,
  next_run_at: null,
  created_at: "2026-09-01T00:00:00Z",
  discord_channel_id: null,
  discord_channel_name: null,
};

function gridCols(el: Element | null): string {
  const cls = el?.className ?? "";
  const m = String(cls).match(/grid-cols-\[[^\]]+\]/);
  return m ? m[0] : "";
}

describe("JobsTable column grid", () => {
  it("uses the same fixed-width actions column in header and rows", () => {
    const noop = vi.fn();
    render(
      <JobsTable
        jobs={[job]}
        onEdit={noop}
        onDelete={noop}
        onTrigger={noop}
        onToggleEnabled={noop}
        onSnooze={noop}
        onDuplicate={noop}
      />,
    );
    const header = screen.getByText("Name").parentElement;
    const row = screen.getByText("Morning digest").closest("[class*='grid-cols-']");
    const headerCols = gridCols(header);
    expect(headerCols).not.toBe("");
    expect(gridCols(row)).toBe(headerCols);
    expect(headerCols).toMatch(/_192px\]$/);
  });
});
