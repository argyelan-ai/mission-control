/**
 * Composer — Task B4 vitest.
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Composer } from "./Composer";
import type { StateEvent, UsageEvent } from "@/lib/chatTypes";
import { C, STATUS } from "@/lib/colors";

function mkUsage(overrides: Partial<UsageEvent> = {}): UsageEvent {
  return {
    kind: "usage",
    uuid: "u1",
    ts: "2026-08-15T10:00:00Z",
    inputTokens: 50_000,
    outputTokens: 1_200,
    model: "claude-sonnet-4-6",
    effort: null,
    contextWindow: 200_000,
    ...overrides,
  };
}

function mkState(status: StateEvent["status"]): StateEvent {
  return { kind: "state", status, prompt: null };
}

describe("Composer", () => {
  it("sends the typed text on Enter and clears the textarea", async () => {
    const user = userEvent.setup();
    const onSend = vi.fn();
    render(
      <Composer agentId="a1" usage={null} state={null} onSend={onSend} onStop={vi.fn()} />
    );
    const textarea = screen.getByPlaceholderText(/Nachricht/);
    await user.type(textarea, "hallo agent{Enter}");
    expect(onSend).toHaveBeenCalledWith("hallo agent");
    expect(textarea).toHaveValue("");
  });

  it("does not send on Shift+Enter and inserts a newline instead", async () => {
    const user = userEvent.setup();
    const onSend = vi.fn();
    render(
      <Composer agentId="a1" usage={null} state={null} onSend={onSend} onStop={vi.fn()} />
    );
    const textarea = screen.getByPlaceholderText(/Nachricht/);
    await user.type(textarea, "zeile1{Shift>}{Enter}{/Shift}zeile2");
    expect(onSend).not.toHaveBeenCalled();
    expect(textarea).toHaveValue("zeile1\nzeile2");
  });

  it("does not send an empty or whitespace-only message", async () => {
    const user = userEvent.setup();
    const onSend = vi.fn();
    render(
      <Composer agentId="a1" usage={null} state={null} onSend={onSend} onStop={vi.fn()} />
    );
    const textarea = screen.getByPlaceholderText(/Nachricht/);
    await user.type(textarea, "   {Enter}");
    expect(onSend).not.toHaveBeenCalled();
  });

  it("shows the prominent Stop button while state.status is working", () => {
    const { rerender } = render(
      <Composer agentId="a1" usage={null} state={mkState("idle")} onSend={vi.fn()} onStop={vi.fn()} />
    );
    expect(screen.queryByTestId("stop-button-prominent")).not.toBeInTheDocument();

    rerender(
      <Composer agentId="a1" usage={null} state={mkState("working")} onSend={vi.fn()} onStop={vi.fn()} />
    );
    expect(screen.getByTestId("stop-button-prominent")).toBeInTheDocument();
    expect(screen.queryByTestId("stop-button-quiet")).not.toBeInTheDocument();
  });

  it("shows a quiet Stop button when the session is live but not confirmed working — sessionLive defaults to true so Boss (no pane probe, mtime heuristic often misses 'working') keeps a way to interrupt", () => {
    render(<Composer agentId="a1" usage={null} state={mkState("idle")} onSend={vi.fn()} onStop={vi.fn()} />);
    expect(screen.getByTestId("stop-button-quiet")).toBeInTheDocument();
    expect(screen.queryByTestId("stop-button-prominent")).not.toBeInTheDocument();
  });

  it('gives the quiet Stop button an "Unterbrechen (ESC)" tooltip', () => {
    render(<Composer agentId="a1" usage={null} state={mkState("idle")} onSend={vi.fn()} onStop={vi.fn()} />);
    expect(screen.getByTestId("stop-button-quiet")).toHaveAttribute("title", "Unterbrechen (ESC)");
  });

  it("hides the Stop button entirely when sessionLive is false, even while working", () => {
    render(
      <Composer agentId="a1" usage={null} state={mkState("working")} sessionLive={false} onSend={vi.fn()} onStop={vi.fn()} />
    );
    expect(screen.queryByTestId("stop-button-prominent")).not.toBeInTheDocument();
    expect(screen.queryByTestId("stop-button-quiet")).not.toBeInTheDocument();
  });

  it("hides the Stop button entirely when sessionLive is false and idle", () => {
    render(
      <Composer agentId="a1" usage={null} state={mkState("idle")} sessionLive={false} onSend={vi.fn()} onStop={vi.fn()} />
    );
    expect(screen.queryByTestId("stop-button-prominent")).not.toBeInTheDocument();
    expect(screen.queryByTestId("stop-button-quiet")).not.toBeInTheDocument();
  });

  it("calls onStop when the prominent Stop button is clicked", async () => {
    const user = userEvent.setup();
    const onStop = vi.fn();
    render(
      <Composer agentId="a1" usage={null} state={mkState("working")} onSend={vi.fn()} onStop={onStop} />
    );
    await user.click(screen.getByTestId("stop-button-prominent"));
    expect(onStop).toHaveBeenCalledTimes(1);
  });

  it("calls onStop when the quiet Stop button is clicked", async () => {
    const user = userEvent.setup();
    const onStop = vi.fn();
    render(
      <Composer agentId="a1" usage={null} state={mkState("idle")} onSend={vi.fn()} onStop={onStop} />
    );
    await user.click(screen.getByTestId("stop-button-quiet"));
    expect(onStop).toHaveBeenCalledTimes(1);
  });

  it("shows the transcript-truth model from usage, not an optimistic guess", () => {
    render(
      <Composer agentId="a1" usage={mkUsage({ model: "claude-opus-4-7" })} state={null} onSend={vi.fn()} onStop={vi.fn()} />
    );
    expect(screen.getByText("claude-opus-4-7")).toBeInTheDocument();
  });

  it('shows "—" for the model chip when there is no usage yet', () => {
    render(<Composer agentId="a1" usage={null} state={null} onSend={vi.fn()} onStop={vi.fn()} />);
    expect(screen.getByText("—")).toBeInTheDocument();
  });

  it("sends /model sonnet when Sonnet is picked from the model dropdown", async () => {
    const user = userEvent.setup();
    const onSend = vi.fn();
    render(
      <Composer agentId="a1" usage={mkUsage()} state={null} onSend={onSend} onStop={vi.fn()} />
    );
    await user.click(screen.getByRole("button", { name: /claude-sonnet-4-6/ }));
    await user.click(screen.getByText("Sonnet"));
    expect(onSend).toHaveBeenCalledWith("/model sonnet");
  });

  it("only shows the effort chip when usage.effort is present", () => {
    const { rerender } = render(
      <Composer agentId="a1" usage={mkUsage({ effort: null })} state={null} onSend={vi.fn()} onStop={vi.fn()} />
    );
    expect(screen.queryByText("high")).not.toBeInTheDocument();

    rerender(
      <Composer agentId="a1" usage={mkUsage({ effort: "high" })} state={null} onSend={vi.fn()} onStop={vi.fn()} />
    );
    expect(screen.getByText("high")).toBeInTheDocument();
  });

  it("prefers usage.usedPct (CLI ground truth) over the contextWindow-based estimate", () => {
    render(
      <Composer
        agentId="a1"
        // If it fell back to the token estimate this would read 50%, not 15%.
        usage={mkUsage({ inputTokens: 100_000, contextWindow: 200_000, usedPct: 15, source: "cli" })}
        state={null}
        onSend={vi.fn()}
        onStop={vi.fn()}
      />
    );
    expect(screen.getByTestId("context-ring-pct")).toHaveTextContent("15%");
    expect(screen.getByTestId("context-ring")).toHaveAttribute("aria-valuenow", "15");
  });

  it("falls back to the inputTokens/contextWindow estimate when usedPct is absent (verified live: Boss on claude-opus-5, 153k used against a 1M window ≈ 15%)", () => {
    render(
      <Composer
        agentId="a1"
        usage={mkUsage({ inputTokens: 153_000, model: "claude-opus-5", contextWindow: 1_000_000 })}
        state={null}
        onSend={vi.fn()}
        onStop={vi.fn()}
      />
    );
    // 153,000 / 1,000,000 = 15.3%, rounded to 15 for display + aria-valuenow.
    expect(screen.getByTestId("context-ring-pct")).toHaveTextContent("15%");
    expect(screen.getByTestId("context-ring")).toHaveAttribute("aria-valuenow", "15");
    expect(screen.getByTestId("context-ring")).toHaveAttribute("data-source", "estimate");
  });

  it("marks the ring's data-source as cli when usedPct is used", () => {
    render(
      <Composer
        agentId="a1"
        usage={mkUsage({ usedPct: 42, source: "cli" })}
        state={null}
        onSend={vi.fn()}
        onStop={vi.fn()}
      />
    );
    expect(screen.getByTestId("context-ring")).toHaveAttribute("data-source", "cli");
  });

  it("renders no ring when there is no usage yet", () => {
    render(<Composer agentId="a1" usage={null} state={null} onSend={vi.fn()} onStop={vi.fn()} />);
    expect(screen.queryByTestId("context-ring")).not.toBeInTheDocument();
  });

  it("renders no ring when neither usedPct nor contextWindow is available — honest absence over a guessed-wrong indicator", () => {
    render(
      <Composer
        agentId="a1"
        usage={mkUsage({ usedPct: null, contextWindow: null })}
        state={null}
        onSend={vi.fn()}
        onStop={vi.fn()}
      />
    );
    expect(screen.queryByTestId("context-ring")).not.toBeInTheDocument();
  });

  it('marks the ring data-threshold "normal" below 75%', () => {
    render(
      <Composer agentId="a1" usage={mkUsage({ usedPct: 50, source: "cli" })} state={null} onSend={vi.fn()} onStop={vi.fn()} />
    );
    expect(screen.getByTestId("context-ring")).toHaveAttribute("data-threshold", "normal");
  });

  it('marks the ring data-threshold "warning" at 75% and above', () => {
    render(
      <Composer agentId="a1" usage={mkUsage({ usedPct: 75, source: "cli" })} state={null} onSend={vi.fn()} onStop={vi.fn()} />
    );
    expect(screen.getByTestId("context-ring")).toHaveAttribute("data-threshold", "warning");
  });

  it('marks the ring data-threshold "error" at 90% and above', () => {
    render(
      <Composer agentId="a1" usage={mkUsage({ usedPct: 90, source: "cli" })} state={null} onSend={vi.fn()} onStop={vi.fn()} />
    );
    expect(screen.getByTestId("context-ring")).toHaveAttribute("data-threshold", "error");
  });

  it("colors the ring arc using STATUS tokens, not raw hex, per threshold", () => {
    const { rerender } = render(
      <Composer agentId="a1" usage={mkUsage({ usedPct: 50, source: "cli" })} state={null} onSend={vi.fn()} onStop={vi.fn()} />
    );
    expect(screen.getByTestId("context-ring-arc")).toHaveAttribute("stroke", C.textDim);

    rerender(
      <Composer agentId="a1" usage={mkUsage({ usedPct: 95, source: "cli" })} state={null} onSend={vi.fn()} onStop={vi.fn()} />
    );
    expect(screen.getByTestId("context-ring-arc")).toHaveAttribute("stroke", STATUS.error);
  });

  it("puts the token detail, source, and explanation sentence in the ring's title tooltip", () => {
    render(
      <Composer
        agentId="a1"
        usage={mkUsage({ inputTokens: 12_345, contextWindow: 200_000, usedPct: 6, source: "cli" })}
        state={null}
        onSend={vi.fn()}
        onStop={vi.fn()}
      />
    );
    expect(screen.getByTestId("context-ring")).toHaveAttribute(
      "title",
      "≈12k/200k belegt. Quelle: CLI. Die CLI-Statuszeile zeigt dagegen den Rest bis zur Auto-Komprimierung an — andere Basis, beide korrekt."
    );
  });

  it('labels the tooltip source "Schätzung" when falling back to the token estimate', () => {
    render(
      <Composer
        agentId="a1"
        usage={mkUsage({ inputTokens: 100_000, contextWindow: 200_000, usedPct: null })}
        state={null}
        onSend={vi.fn()}
        onStop={vi.fn()}
      />
    );
    expect(screen.getByTestId("context-ring")).toHaveAttribute(
      "title",
      expect.stringContaining("Quelle: Schätzung")
    );
  });

  it('opens the slash-command palette when "/" is typed at position 0, listing all commands', async () => {
    const user = userEvent.setup();
    render(<Composer agentId="a1" usage={null} state={null} onSend={vi.fn()} onStop={vi.fn()} />);
    const textarea = screen.getByPlaceholderText(/Nachricht/);
    await user.type(textarea, "/");
    expect(screen.getByText("/clear")).toBeInTheDocument();
    expect(screen.getByText("/model")).toBeInTheDocument();
  });

  it("does not open the palette for a / that isn't the first character", async () => {
    const user = userEvent.setup();
    render(<Composer agentId="a1" usage={null} state={null} onSend={vi.fn()} onStop={vi.fn()} />);
    const textarea = screen.getByPlaceholderText(/Nachricht/);
    await user.type(textarea, "path/to/file");
    expect(screen.queryByText("/clear")).not.toBeInTheDocument();
  });

  it('filters live as you type — "/mo" narrows to only /model', async () => {
    const user = userEvent.setup();
    render(<Composer agentId="a1" usage={null} state={null} onSend={vi.fn()} onStop={vi.fn()} />);
    const textarea = screen.getByPlaceholderText(/Nachricht/);
    await user.type(textarea, "/mo");
    expect(screen.getByText("/model")).toBeInTheDocument();
    expect(screen.queryByText("/clear")).not.toBeInTheDocument();
    expect(screen.queryByText("/compact")).not.toBeInTheDocument();
    expect(screen.queryByText("/context")).not.toBeInTheDocument();
  });

  it("filters case-insensitively", async () => {
    const user = userEvent.setup();
    render(<Composer agentId="a1" usage={null} state={null} onSend={vi.fn()} onStop={vi.fn()} />);
    const textarea = screen.getByPlaceholderText(/Nachricht/);
    await user.type(textarea, "/MO");
    expect(screen.getByText("/model")).toBeInTheDocument();
  });

  it("closes the palette entirely when the filter matches nothing", async () => {
    const user = userEvent.setup();
    render(<Composer agentId="a1" usage={null} state={null} onSend={vi.fn()} onStop={vi.fn()} />);
    const textarea = screen.getByPlaceholderText(/Nachricht/);
    await user.type(textarea, "/xyz");
    expect(screen.queryByTestId("slash-palette")).not.toBeInTheDocument();
  });

  it("resets the highlight to the first match on every keystroke", async () => {
    const user = userEvent.setup();
    render(<Composer agentId="a1" usage={null} state={null} onSend={vi.fn()} onStop={vi.fn()} />);
    const textarea = screen.getByPlaceholderText(/Nachricht/);
    await user.type(textarea, "/c");
    await user.keyboard("{ArrowDown}"); // highlight moves to the 2nd "/c…" match
    await user.type(textarea, "o"); // narrows to "/co…" — highlight must snap back to index 0
    const first = screen.getAllByText(/^\/co/, { selector: "span" })[0].closest("button");
    expect(first).toHaveAttribute("data-highlighted", "true");
  });

  it("moves the highlight with ArrowDown/ArrowUp", async () => {
    const user = userEvent.setup();
    render(<Composer agentId="a1" usage={null} state={null} onSend={vi.fn()} onStop={vi.fn()} />);
    const textarea = screen.getByPlaceholderText(/Nachricht/);
    await user.type(textarea, "/");
    expect(screen.getByTestId("slash-item-/model")).toHaveAttribute("data-highlighted", "true");

    await user.keyboard("{ArrowDown}");
    expect(screen.getByTestId("slash-item-/model")).toHaveAttribute("data-highlighted", "false");
    expect(screen.getByTestId("slash-item-/clear")).toHaveAttribute("data-highlighted", "true");

    await user.keyboard("{ArrowUp}");
    expect(screen.getByTestId("slash-item-/model")).toHaveAttribute("data-highlighted", "true");
  });

  it("inserts the highlighted command on Enter and does NOT send", async () => {
    const user = userEvent.setup();
    const onSend = vi.fn();
    render(<Composer agentId="a1" usage={null} state={null} onSend={onSend} onStop={vi.fn()} />);
    const textarea = screen.getByPlaceholderText(/Nachricht/);
    await user.type(textarea, "/mo{Enter}");
    expect(textarea).toHaveValue("/model ");
    expect(onSend).not.toHaveBeenCalled();
  });

  it("inserts the highlighted command on Tab", async () => {
    const user = userEvent.setup();
    render(<Composer agentId="a1" usage={null} state={null} onSend={vi.fn()} onStop={vi.fn()} />);
    const textarea = screen.getByPlaceholderText(/Nachricht/);
    await user.type(textarea, "/mo");
    await user.keyboard("{Tab}");
    expect(textarea).toHaveValue("/model ");
  });

  it("Escape closes the palette without clearing the input", async () => {
    const user = userEvent.setup();
    render(<Composer agentId="a1" usage={null} state={null} onSend={vi.fn()} onStop={vi.fn()} />);
    const textarea = screen.getByPlaceholderText(/Nachricht/);
    await user.type(textarea, "/mo");
    await user.keyboard("{Escape}");
    expect(screen.queryByTestId("slash-palette")).not.toBeInTheDocument();
    expect(textarea).toHaveValue("/mo");
  });

  it("inserts the clicked command into the textarea and closes the palette", async () => {
    const user = userEvent.setup();
    render(<Composer agentId="a1" usage={null} state={null} onSend={vi.fn()} onStop={vi.fn()} />);
    const textarea = screen.getByPlaceholderText(/Nachricht/);
    await user.type(textarea, "/");
    await user.click(screen.getByText("/compact"));
    expect(textarea).toHaveValue("/compact ");
    expect(screen.queryByText("/clear")).not.toBeInTheDocument();
  });

  it("anchors the palette directly above the input via absolute positioning (bottom-full) with a 320px floor", async () => {
    const user = userEvent.setup();
    render(<Composer agentId="a1" usage={null} state={null} onSend={vi.fn()} onStop={vi.fn()} />);
    const textarea = screen.getByPlaceholderText(/Nachricht/);
    await user.type(textarea, "/");
    const palette = screen.getByTestId("slash-palette");
    // Inline style, not just the class — see the comment in Composer.tsx on
    // why relying on the Tailwind class alone previously broke this.
    expect(palette.style.position).toBe("absolute");
    expect(palette.className).toContain("bottom-full");
    expect(palette.className).toContain("left-3");
    expect(palette.className).toContain("right-3");
    expect(palette.className).toContain("min-w-[320px]");
  });
});
