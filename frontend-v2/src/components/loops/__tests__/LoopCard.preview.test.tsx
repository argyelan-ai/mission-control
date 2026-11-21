/** The loop card's goal preview shows words, not Markdown marks. */
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { LoopCard } from "../LoopCard";
import type { Loop } from "@/lib/types";

const loop = {
  id: "l1", board_id: "b1", project_id: null, name: "Docs loop",
  goal: "## Ziel\n\n**Alle** Seiten haben eine [Anleitung](https://example.com) und `npm test` ist grün.",
  backlog_source: "markdown", backlog_md: null, backlog_tag: null, round_brief: null,
  human_every_n_rounds: 0, pause_on_failed_rounds: 2, escalate_on: null, max_rounds: 5,
  max_duration_minutes: null, stop_on_backlog_empty: true, telegram_reports: false,
  status: "draft", rounds_completed: 0, consecutive_failed_rounds: 0, current_round_no: null,
  current_task_id: null, last_error: null, started_at: null,
} as unknown as Loop;

describe("LoopCard goal preview", () => {
  it("strips Markdown marks from the goal", () => {
    render(<LoopCard loop={loop} onOpen={vi.fn()} onStart={vi.fn()} onPause={vi.fn()} onStop={vi.fn()} onDelete={vi.fn()} />);
    const p = screen.getByText(/Seiten haben eine Anleitung/);
    expect(p.textContent).toBe("Ziel Alle Seiten haben eine Anleitung und npm test ist grün.");
    expect(p.textContent).not.toMatch(/[#*`[\]]/);
  });
});
