import { describe, it, expect, vi, afterEach } from "vitest";
import { act, render, screen } from "@testing-library/react";
import { fireEvent } from "@testing-library/react";
import { LiveThinkingRow } from "./LiveThinkingRow";
import type { PreviewEvent } from "@/lib/chatTypes";

const NOW = new Date("2026-10-20T09:05:00.000Z");

function thought(overrides: Partial<PreviewEvent> = {}): PreviewEvent {
  return {
    kind: "preview", uuid: null, ts: "2026-10-20T09:04:59.000Z", source: "acp",
    block: "thinking", chars: 23_600, startedAt: "2026-10-20T09:03:15.000Z",
    text: "…so the checklist binds to the current task. Plan: build a single list.",
    ...overrides,
  };
}

describe("LiveThinkingRow", () => {
  afterEach(() => vi.useRealTimers());

  it("says the model is thinking, how much and for how long — collapsed, text hidden", () => {
    vi.useFakeTimers({ now: NOW });
    render(<LiveThinkingRow preview={thought()} />);
    const row = screen.getByTestId("live-thinking");
    expect(row).toHaveTextContent("Thinking");
    expect(row).toHaveTextContent("≈5,900 tokens");
    expect(row).toHaveTextContent("1:45");
    expect(screen.queryByText(/Plan: build a single list/)).not.toBeInTheDocument();
    expect(screen.getByRole("button")).toHaveAttribute("aria-expanded", "false");
  });

  it("counts up every second while the thought runs", () => {
    vi.useFakeTimers({ now: NOW });
    render(<LiveThinkingRow preview={thought()} />);
    expect(screen.getByTestId("live-thinking")).toHaveTextContent("1:45");
    act(() => { vi.advanceTimersByTime(3_000); });
    expect(screen.getByTestId("live-thinking")).toHaveTextContent("1:48");
  });

  it("opens to the streamed end of the thought", () => {
    vi.useFakeTimers({ now: NOW });
    render(<LiveThinkingRow preview={thought()} />);
    fireEvent.click(screen.getByRole("button"));
    expect(screen.getByRole("button")).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText(/Plan: build a single list/)).toBeInTheDocument();
  });

  it("without a size or a start it still says Thinking — nothing invented", () => {
    vi.useFakeTimers({ now: NOW });
    render(<LiveThinkingRow preview={thought({ chars: undefined, startedAt: undefined })} />);
    const row = screen.getByTestId("live-thinking");
    expect(row).toHaveTextContent("Thinking");
    expect(row).not.toHaveTextContent("tokens");
    expect(row).not.toHaveTextContent(":");
  });

  it("stops counting when nothing new came for a minute — a dead engine is not shown as work", () => {
    vi.useFakeTimers({ now: NOW });
    render(<LiveThinkingRow preview={thought({ ts: "2026-10-20T09:03:45.000Z" })} />);
    const row = screen.getByTestId("live-thinking");
    // started 09:03:15, last piece 09:03:45, now 09:05:00 -> clock frozen at 0:30
    expect(row).toHaveTextContent("0:30");
    // Review UI-6 (K10): the repo's one form for "how long silent" — a span,
    // never a second m:ss clock that reads like a time of day after "since".
    expect(row).toHaveTextContent("silent for 1 min");
    expect(row).not.toHaveTextContent("1:15");
    act(() => { vi.advanceTimersByTime(5_000); });
    expect(row).toHaveTextContent("0:30");
    expect(row).toHaveTextContent("silent for 1 min");
    act(() => { vi.advanceTimersByTime(60_000); });
    expect(row).toHaveTextContent("silent for 2 min");
  });

  // Review R4/UI-3: a live region around a clock read "1:47", "1:48", … to a
  // screen reader every second for the whole thought.
  it("no live region ticks with the clock; one calm status says thinking / silent", () => {
    vi.useFakeTimers({ now: NOW });
    const { container } = render(<LiveThinkingRow preview={thought()} />);
    expect(screen.getByTestId("live-thinking")).not.toHaveAttribute("aria-live");
    const live = container.querySelectorAll("[aria-live]:not([aria-live=off]), [role=status]");
    expect(live).toHaveLength(1);
    const status = screen.getByRole("status");
    const before = status.textContent;
    expect(before).toContain("Thinking");
    expect(before).not.toMatch(/\d:\d\d/);
    act(() => { vi.advanceTimersByTime(3_000); });
    expect(status.textContent).toBe(before);
    fireEvent.click(screen.getByRole("button"));
    expect(screen.getByTestId("live-thinking-text").closest("[aria-live]:not([aria-live=off]), [role=status]")).toBeNull();
  });

  it("a silent thought says so once in the status, without a ticking clock", () => {
    vi.useFakeTimers({ now: NOW });
    render(<LiveThinkingRow preview={thought({ ts: "2026-10-20T09:03:45.000Z" })} />);
    const status = screen.getByRole("status");
    expect(status).toHaveTextContent("silent for 1 min");
    const before = status.textContent;
    act(() => { vi.advanceTimersByTime(5_000); });
    expect(status.textContent).toBe(before);
  });

  // Review UI-7 (K9): same icon and indent as ToolRow / ThinkingRow (13 px +
  // gap-2), not 14 px + ml-6 — three edges 1–3 px apart.
  it("lines up with the step rows: 13 px icon, text indented by icon + gap", () => {
    vi.useFakeTimers({ now: NOW });
    const { container } = render(<LiveThinkingRow preview={thought()} />);
    const icon = container.querySelector("button svg");
    expect(icon).toHaveAttribute("width", "13");
    fireEvent.click(screen.getByRole("button"));
    const open = screen.getByTestId("live-thinking-text");
    expect(open.className).not.toMatch(/\bml-6\b/);
    expect(open.className).toMatch(/\bgap-2\b/);
    expect(open.querySelector("[data-indent]")).toHaveClass("w-[13px]");
  });
});
