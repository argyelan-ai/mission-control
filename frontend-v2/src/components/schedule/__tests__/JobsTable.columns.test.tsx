/**
 * Schedule jobs table — header and rows must share one column grid.
 *
 * Header and each row are separate CSS grids with the same template. The last
 * (actions) column used to be `auto`: rows hold ~190 px of hover buttons
 * there, the header holds nothing, so every `fr` column ended up a different
 * width and the header labels drifted away from their values.
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
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
    // The phone card (md:hidden) holds the name too — the grid lives in the table.
    const table = within(screen.getByRole("region", { name: "Jobs table" }));
    const header = table.getByText("Name").parentElement;
    const row = table.getByText("Morning digest").closest("[class*='grid-cols-']");
    const headerCols = gridCols(header);
    expect(headerCols).not.toBe("");
    expect(gridCols(row)).toBe(headerCols);
    expect(headerCols).toMatch(/_192px\]$/);
  });
});

describe("JobsTable in a narrow content column", () => {
  const noop = vi.fn();
  const props = { onEdit: noop, onDelete: noop, onTrigger: noop, onToggleEnabled: noop, onSnooze: noop, onDuplicate: noop };

  it("switches cards/table on the width of its own column, not the viewport", () => {
    // Beside the 256 px sidebar the viewport says little about the room the
    // table gets (1100 px viewport ≈ 760 px column, still cut). The table's
    // root is a size container and the switch reads its width.
    const { container } = render(<JobsTable jobs={[job]} {...props} />);
    expect((container.firstElementChild as HTMLElement).className).toContain("@container");
    const table = screen.getByRole("region", { name: "Jobs table" });
    expect(table.className).toContain("hidden");
    expect(table.className).toContain("@min-[880px]:block");
    const cards = screen.getByTestId("job-card").parentElement!;
    expect(cards.className).toContain("@min-[880px]:hidden");
    expect(`${table.className} ${cards.className}`).not.toMatch(/(^|\s)min-\[\d+px\]:/);
  });

  it("gives Last run more width than Agent", () => {
    render(<JobsTable jobs={[job]} {...props} />);
    const header = within(screen.getByRole("region", { name: "Jobs table" })).getByText("Name").parentElement;
    const tracks = gridCols(header).replace(/^grid-cols-\[|\]$/g, "").split("_");
    const fr = (t: string) => Number((t.match(/([\d.]+)fr/) ?? [])[1] ?? 0);
    // checkbox · status · name · trigger · next · last · agent · actions
    expect(fr(tracks[5])).toBeGreaterThan(fr(tracks[6]));
  });
});
