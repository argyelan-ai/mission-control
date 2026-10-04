/**
 * HeadChatRow — the Chats list row for a head (bauplan `heads-sichtbar`
 * PR 2 §3.2). One state word / time segment per line (K15), pair shown for
 * active runs, dropped for ended ones.
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { HeadChatRow } from "../HeadChatRow";
import { mkRun } from "@/lib/__tests__/headFixtures";

describe("HeadChatRow", () => {
  it("needs_you shows a single word, no pair, no time — never two segments for one fact", () => {
    render(<HeadChatRow run={mkRun({ state: "needs_you" })} selected={false} onSelect={vi.fn()} />);
    const row = screen.getByTestId("head-chat-row");
    expect(row).toHaveTextContent("Needs you");
    expect(row).not.toHaveTextContent("omp");
    expect(row.textContent?.split("·").length).toBe(1);
  });

  it("running shows the pair and a running-for duration", () => {
    const now = Date.parse("2026-09-23T10:12:00Z");
    vi.spyOn(Date, "now").mockReturnValue(now);
    render(
      <HeadChatRow
        run={mkRun({ state: "running", harness: "omp", model: "GLM-5.3-Flash-EXL3", started_at: "2026-09-23T10:00:00Z" })}
        selected={false}
        onSelect={vi.fn()}
      />,
    );
    const row = screen.getByTestId("head-chat-row");
    expect(row).toHaveTextContent("omp × GLM-5.3");
    expect(row).toHaveTextContent("running for 12 min");
    vi.restoreAllMocks();
  });

  it("starting shows the pair with 'starting…', no duration yet", () => {
    render(<HeadChatRow run={mkRun({ state: "starting" })} selected={false} onSelect={vi.fn()} />);
    expect(screen.getByTestId("head-chat-row")).toHaveTextContent("starting…");
  });

  it("an ended run shows the state word and age, never the pair (K15: one dimension per segment)", () => {
    render(
      <HeadChatRow
        run={mkRun({ state: "passed", exited_at: "2026-09-20T10:00:00Z" })}
        selected={false}
        onSelect={vi.fn()}
      />,
    );
    const row = screen.getByTestId("head-chat-row");
    expect(row).toHaveTextContent("Passed");
    expect(row).toHaveTextContent("ago");
    expect(row).not.toHaveTextContent("omp ×");
  });

  it("strips a leading bracket tag from the title", () => {
    render(<HeadChatRow run={mkRun({ title: "[night] Fix flaky retry test" })} selected={false} onSelect={vi.fn()} />);
    expect(screen.getByTestId("head-chat-row")).toHaveTextContent("Fix flaky retry test");
    expect(screen.getByTestId("head-chat-row")).not.toHaveTextContent("[night]");
  });

  it("calls onSelect with the run id when clicked", async () => {
    const onSelect = vi.fn();
    render(<HeadChatRow run={mkRun({ run_id: "r-9" })} selected={false} onSelect={onSelect} />);
    screen.getByTestId("head-chat-row").click();
    expect(onSelect).toHaveBeenCalledWith("r-9");
  });
});
