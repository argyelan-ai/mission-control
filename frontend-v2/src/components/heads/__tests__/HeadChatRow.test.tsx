/**
 * HeadChatRow — the Chats list row for a head (bauplan `heads-sichtbar`
 * PR 2 §3.2). Pair + exactly one state-word segment per line, for every
 * state including needs_you/ended (review finding on PR #756: the pair was
 * dropped there entirely, citing an un-adopted rule proposal, K15 — see
 * anhang.md H — leaving no way to tell which harness ran a finished or
 * waiting head anywhere in Chats).
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { HeadChatRow } from "../HeadChatRow";
import { mkRun } from "@/lib/__tests__/headFixtures";

describe("HeadChatRow", () => {
  it("needs_you shows the pair and exactly one state segment — never two words for the same fact", () => {
    render(<HeadChatRow run={mkRun({ state: "needs_you", harness: "omp", model: "GLM-5.3-Flash-EXL3" })} selected={false} onSelect={vi.fn()} />);
    const row = screen.getByTestId("head-chat-row");
    expect(row).toHaveTextContent("omp × GLM-5.3");
    // Lowercase mid-line (review finding on PR #756 round 3, K10): the
    // running row's own "running for …" is lowercase, so the state word
    // matches it here instead of standing out as "Needs you" — the i18n
    // value itself stays sentence case for the surfaces that show it
    // standalone (HeadStateCard's badge etc.).
    expect(row).toHaveTextContent("needs you");
    expect(row).not.toHaveTextContent("Needs you");
    // One "·" between pair and state — not a second one repeating/
    // qualifying the same "needs you" fact (the actual anhang.md H finding).
    expect(row.textContent?.split("·").length).toBe(2);
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

  it("an ended run shows the pair, the state word and the age", () => {
    const now = Date.parse("2026-09-23T10:00:00Z");
    vi.spyOn(Date, "now").mockReturnValue(now);
    render(
      <HeadChatRow
        run={mkRun({ state: "passed", harness: "omp", model: "GLM-5.3-Flash-EXL3", exited_at: "2026-09-20T10:00:00Z" })}
        selected={false}
        onSelect={vi.fn()}
      />,
    );
    const row = screen.getByTestId("head-chat-row");
    expect(row).toHaveTextContent("omp × GLM-5.3");
    expect(row).toHaveTextContent("passed");
    expect(row).not.toHaveTextContent("Passed");
    expect(row).toHaveTextContent("ago");
    vi.restoreAllMocks();
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
