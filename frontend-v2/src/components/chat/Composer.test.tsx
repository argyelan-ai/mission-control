/**
 * Composer — Task B4 vitest.
 *
 * cmdk's <Command.List> reaches for `ResizeObserver` and its item-selection
 * effect calls `Element.scrollIntoView` — neither exists in jsdom, so both
 * are stubbed here (scoped to this file, not the shared test-setup, since
 * other in-flight chat components don't need them).
 */
import { describe, it, expect, vi, beforeAll } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Composer } from "./Composer";
import type { StateEvent, UsageEvent } from "@/lib/chatTypes";

beforeAll(() => {
  window.HTMLElement.prototype.scrollIntoView = vi.fn();
  class MockResizeObserver {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  window.ResizeObserver = MockResizeObserver;
});

function mkUsage(overrides: Partial<UsageEvent> = {}): UsageEvent {
  return {
    kind: "usage",
    uuid: "u1",
    ts: "2026-08-15T10:00:00Z",
    inputTokens: 50_000,
    outputTokens: 1_200,
    model: "claude-sonnet-4-6",
    effort: null,
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

  it("shows the Stop button only while state.status is working", () => {
    const { rerender } = render(
      <Composer agentId="a1" usage={null} state={mkState("idle")} onSend={vi.fn()} onStop={vi.fn()} />
    );
    expect(screen.queryByRole("button", { name: /stop/i })).not.toBeInTheDocument();

    rerender(
      <Composer agentId="a1" usage={null} state={mkState("working")} onSend={vi.fn()} onStop={vi.fn()} />
    );
    expect(screen.getByRole("button", { name: /stop/i })).toBeInTheDocument();
  });

  it("calls onStop when the Stop button is clicked", async () => {
    const user = userEvent.setup();
    const onStop = vi.fn();
    render(
      <Composer agentId="a1" usage={null} state={mkState("working")} onSend={vi.fn()} onStop={onStop} />
    );
    await user.click(screen.getByRole("button", { name: /stop/i }));
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

  it("fills the context meter proportionally to usage.inputTokens / contextWindow(model)", () => {
    render(
      <Composer
        agentId="a1"
        usage={mkUsage({ inputTokens: 100_000, model: "claude-sonnet-4-6" })}
        state={null}
        onSend={vi.fn()}
        onStop={vi.fn()}
      />
    );
    const fill = screen.getByTestId("context-meter-fill");
    // 100,000 / 200,000 (standard window) = 50%
    expect(fill.style.width).toBe("50%");
  });

  it("uses the 1M window for [1m] model variants in the meter", () => {
    render(
      <Composer
        agentId="a1"
        usage={mkUsage({ inputTokens: 500_000, model: "claude-sonnet-4-6[1m]" })}
        state={null}
        onSend={vi.fn()}
        onStop={vi.fn()}
      />
    );
    const fill = screen.getByTestId("context-meter-fill");
    expect(fill.style.width).toBe("50%");
  });

  it("puts the exact token numbers in the meter's title tooltip", () => {
    render(
      <Composer
        agentId="a1"
        usage={mkUsage({ inputTokens: 12_345, model: "claude-sonnet-4-6" })}
        state={null}
        onSend={vi.fn()}
        onStop={vi.fn()}
      />
    );
    expect(screen.getByTestId("context-meter")).toHaveAttribute(
      "title",
      "12,345 / 200,000 Tokens"
    );
  });

  it('opens the slash-command palette when "/" is typed at position 0', async () => {
    const user = userEvent.setup();
    render(<Composer agentId="a1" usage={null} state={null} onSend={vi.fn()} onStop={vi.fn()} />);
    const textarea = screen.getByPlaceholderText(/Nachricht/);
    await user.type(textarea, "/");
    expect(screen.getByText("/clear")).toBeInTheDocument();
  });

  it("does not open the palette for a / that isn't the first character", async () => {
    const user = userEvent.setup();
    render(<Composer agentId="a1" usage={null} state={null} onSend={vi.fn()} onStop={vi.fn()} />);
    const textarea = screen.getByPlaceholderText(/Nachricht/);
    await user.type(textarea, "path/to/file");
    expect(screen.queryByText("/clear")).not.toBeInTheDocument();
  });

  it("inserts the picked command into the textarea and closes the palette", async () => {
    const user = userEvent.setup();
    render(<Composer agentId="a1" usage={null} state={null} onSend={vi.fn()} onStop={vi.fn()} />);
    const textarea = screen.getByPlaceholderText(/Nachricht/);
    await user.type(textarea, "/");
    await user.click(screen.getByText("/compact"));
    expect(textarea).toHaveValue("/compact ");
    expect(screen.queryByText("/clear")).not.toBeInTheDocument();
  });
});
