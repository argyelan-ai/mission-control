/**
 * Composer — Task B4 vitest.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Composer } from "./Composer";
import type { StateEvent, UsageEvent } from "@/lib/chatTypes";
import { C, STATUS } from "@/lib/colors";
import { api } from "@/lib/api";
import { notify } from "@/lib/notify";

// The effort chip talks to the backend directly (it is the only control in the
// composer that does), so both the client and the toast helper are stubbed.
vi.mock("@/lib/api", () => ({ api: { chat: { setEffort: vi.fn() } } }));
vi.mock("@/lib/notify", () => ({
  notify: { error: vi.fn(), success: vi.fn(), info: vi.fn(), warning: vi.fn() },
}));

const mockSetEffort = vi.mocked(api.chat.setEffort);
const mockNotifyError = vi.mocked(notify.error);
const mockNotifyInfo = vi.mocked(notify.info);

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
  beforeEach(() => {
    vi.clearAllMocks();
    mockSetEffort.mockResolvedValue(undefined);
  });

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

  // ── Context ring → breakdown popover ──────────────────────────────────────

  it("opens the context breakdown when the ring is clicked", async () => {
    const user = userEvent.setup();
    render(
      <Composer
        agentId="a1"
        usage={mkUsage({
          components: { input: 10_000, cacheRead: 38_000, cacheCreation: 2_000, output: 1_000 },
        })}
        state={null}
        onSend={vi.fn()}
        onStop={vi.fn()}
      />
    );
    expect(screen.queryByTestId("context-panel")).not.toBeInTheDocument();

    const trigger = screen.getByRole("button", { name: /^Kontext: \d+% belegt$/ });
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    await user.click(trigger);

    expect(screen.getByTestId("context-panel")).toBeInTheDocument();
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByTestId("context-row-cacheRead")).toHaveTextContent("Cache gelesen");
  });

  it("puts the fill percentage in the trigger's accessible name, and keeps it current", () => {
    // A `role="progressbar"` nested inside a button is unreachable — the
    // button's own name is what gets announced, so the figure must live there.
    const { rerender } = render(
      <Composer agentId="a1" usage={mkUsage({ usedPct: 15 })} state={null} onSend={vi.fn()} onStop={vi.fn()} />
    );
    expect(screen.getByRole("button", { name: "Kontext: 15% belegt" })).toBeInTheDocument();

    rerender(
      <Composer agentId="a1" usage={mkUsage({ usedPct: 92 })} state={null} onSend={vi.fn()} onStop={vi.fn()} />
    );
    expect(screen.getByRole("button", { name: "Kontext: 92% belegt" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Kontext: 15% belegt" })).not.toBeInTheDocument();
  });

  it("keeps the ring itself a progressbar with its tooltip (quick glance stays)", () => {
    render(<Composer agentId="a1" usage={mkUsage()} state={null} onSend={vi.fn()} onStop={vi.fn()} />);
    const ring = screen.getByTestId("context-ring");
    expect(ring).toHaveAttribute("role", "progressbar");
    expect(ring).toHaveAttribute("title");
  });

  it("does not offer the breakdown when there is no usage to break down", () => {
    render(<Composer agentId="a1" usage={null} state={null} onSend={vi.fn()} onStop={vi.fn()} />);
    expect(screen.queryByRole("button", { name: /Kontext:/ })).not.toBeInTheDocument();
  });

  it("keeps the send button a ghost outline until there is something to send", async () => {
    const user = userEvent.setup();
    render(<Composer agentId="a1" usage={null} state={null} onSend={vi.fn()} onStop={vi.fn()} />);
    const send = screen.getByRole("button", { name: "Senden" });
    // The accent means "this is the action" — there is no action yet.
    expect(send).toBeDisabled();
    expect(send).toHaveAttribute("data-empty", "true");
    expect(send.style.backgroundColor).toBe("transparent");

    await user.type(screen.getByPlaceholderText(/Nachricht/), "los");
    expect(send).toBeEnabled();
    expect(send).toHaveAttribute("data-empty", "false");
    expect(send.style.backgroundColor).not.toBe("transparent");
  });

  // Regression: the mobile stack keeps the off-screen pane mounted with
  // `display: none`. There the textarea measures scrollHeight 0, and writing
  // that back pinned the input to its padding height (12px) for good — the
  // auto-grow effect only re-runs on `text`. Leaving the height unset keeps the
  // natural rows=1 box. jsdom reports 0 for every metric, so it stands in for
  // the hidden case exactly.
  it("does not pin the textarea height while the element cannot be measured", () => {
    render(<Composer agentId="a1" usage={null} state={null} onSend={vi.fn()} onStop={vi.fn()} />);
    const textarea = screen.getByPlaceholderText(/Nachricht/) as HTMLTextAreaElement;
    expect(textarea.style.height).toBe("");
  });

  it("reports the model dropdown's open state to assistive tech", async () => {
    const user = userEvent.setup();
    render(<Composer agentId="a1" usage={mkUsage()} state={null} onSend={vi.fn()} onStop={vi.fn()} />);
    const trigger = screen.getByRole("button", { name: /claude-sonnet-4-6/ });
    expect(trigger).toHaveAttribute("aria-expanded", "false");

    await user.click(trigger);
    expect(trigger).toHaveAttribute("aria-expanded", "true");
  });

  it("marks the running model as the selected option in the dropdown", async () => {
    const user = userEvent.setup();
    // CLAUDE_MODELS entries are short names ("sonnet"); the running model here
    // is one of them so the list can show which one is live.
    render(
      <Composer agentId="a1" usage={mkUsage({ model: "sonnet" })} state={null} onSend={vi.fn()} onStop={vi.fn()} />
    );
    await user.click(screen.getByRole("button", { name: /sonnet/ }));
    expect(screen.getByRole("option", { name: "Sonnet" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("option", { name: "Opus" })).toHaveAttribute("aria-selected", "false");
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

  // ── Effort switching ──────────────────────────────────────────────────────

  describe("effort chip", () => {
    function renderWithEffort(effort: string | null = "medium") {
      return render(
        <Composer
          agentId="a1"
          usage={mkUsage({ effort })}
          state={null}
          onSend={vi.fn()}
          onStop={vi.fn()}
        />
      );
    }

    it("offers exactly the three switchable levels", async () => {
      const user = userEvent.setup();
      renderWithEffort();
      await user.click(screen.getByTestId("effort-chip"));

      const options = screen.getAllByRole("option");
      expect(options.map((o) => o.textContent?.replace("…", "").trim())).toEqual([
        "low",
        "medium",
        "high",
      ]);
    });

    it("marks the level the transcript reports as the selected one", async () => {
      const user = userEvent.setup();
      renderWithEffort("high");
      await user.click(screen.getByTestId("effort-chip"));

      expect(screen.getByRole("option", { name: /high/ })).toHaveAttribute("aria-selected", "true");
      expect(screen.getByRole("option", { name: /low/ })).toHaveAttribute("aria-selected", "false");
    });

    it("sends the chosen level to the backend", async () => {
      const user = userEvent.setup();
      renderWithEffort("medium");
      await user.click(screen.getByTestId("effort-chip"));
      await user.click(screen.getByRole("option", { name: /high/ }));

      expect(mockSetEffort).toHaveBeenCalledWith("a1", "high");
    });

    it("does not call the backend when the current level is re-picked", async () => {
      const user = userEvent.setup();
      renderWithEffort("medium");
      await user.click(screen.getByTestId("effort-chip"));
      await user.click(screen.getByRole("option", { name: /medium/ }));

      expect(mockSetEffort).not.toHaveBeenCalled();
    });

    it("warns that the switch outlives this session", async () => {
      const user = userEvent.setup();
      renderWithEffort();
      await user.click(screen.getByTestId("effort-chip"));

      expect(screen.getByText("Gilt als neuer Standard des Agenten.")).toBeInTheDocument();
    });

    it("keeps showing the transcript's level while the switch is in flight", async () => {
      let resolve: (() => void) | undefined;
      mockSetEffort.mockImplementation(() => new Promise<void>((r) => { resolve = () => r(); }));
      const user = userEvent.setup();
      renderWithEffort("medium");

      await user.click(screen.getByTestId("effort-chip"));
      await user.click(screen.getByRole("option", { name: /high/ }));

      const chip = screen.getByTestId("effort-chip");
      // No fake instant flip: the label is still what the agent last reported.
      expect(chip).toHaveTextContent("medium");
      expect(chip).toHaveAttribute("data-pending", "true");
      expect(chip).toBeDisabled();

      resolve?.();
      await waitFor(() => expect(screen.getByTestId("effort-chip")).toHaveAttribute("data-pending", "false"));
    });

    it("demotes the chip to a read-only value when the runtime has no terminal to drive", async () => {
      mockSetEffort.mockRejectedValue(new Error('API 409: {"reason":"input_not_supported"}'));
      const user = userEvent.setup();
      renderWithEffort("medium");
      await user.click(screen.getByTestId("effort-chip"));
      await user.click(screen.getByRole("option", { name: /high/ }));

      await waitFor(() => expect(screen.getByTestId("effort-chip-static")).toBeInTheDocument());
      expect(screen.queryByTestId("effort-chip")).not.toBeInTheDocument();
      expect(screen.getByTestId("effort-chip-static")).toHaveAttribute("title", expect.stringContaining("Runtime"));
      // Not an error the operator caused — no toast.
      expect(mockNotifyError).not.toHaveBeenCalled();
    });

    it("treats a busy agent as a wrong moment, not a failure", async () => {
      // The backend refuses mid-turn on purpose (its own preflight, so a
      // working agent never gets interrupted). A red persistent error toast
      // would tell the operator something broke when nothing did.
      mockSetEffort.mockRejectedValue(new Error('API 409: {"reason":"agent_busy"}'));
      const user = userEvent.setup();
      renderWithEffort("medium");
      await user.click(screen.getByTestId("effort-chip"));
      await user.click(screen.getByRole("option", { name: /high/ }));

      await waitFor(() =>
        expect(mockNotifyInfo).toHaveBeenCalledWith(
          "Agent arbeitet gerade — nach dem Zug erneut versuchen"
        )
      );
      expect(mockNotifyError).not.toHaveBeenCalled();
      // Still switchable — the runtime supports it, this attempt was just early.
      expect(screen.getByTestId("effort-chip")).toBeInTheDocument();
      expect(screen.getByTestId("effort-chip")).toHaveAttribute("data-pending", "false");
    });

    it("surfaces an unverified switch as an error and keeps the chip interactive", async () => {
      mockSetEffort.mockRejectedValue(new Error('API 409: {"reason":"effort_switch_failed"}'));
      const user = userEvent.setup();
      renderWithEffort("medium");
      await user.click(screen.getByTestId("effort-chip"));
      await user.click(screen.getByRole("option", { name: /high/ }));

      await waitFor(() => expect(mockNotifyError).toHaveBeenCalledWith(
        "Effort-Wechsel nicht bestätigt — im Terminal prüfen"
      ));
      const chip = screen.getByTestId("effort-chip");
      expect(chip).toHaveTextContent("medium");
      expect(chip).toHaveAttribute("data-pending", "false");
    });

    it("surfaces any other failure without demoting the chip", async () => {
      mockSetEffort.mockRejectedValue(new Error("API 500: boom"));
      const user = userEvent.setup();
      renderWithEffort("medium");
      await user.click(screen.getByTestId("effort-chip"));
      await user.click(screen.getByRole("option", { name: /high/ }));

      await waitFor(() => expect(mockNotifyError).toHaveBeenCalledWith("Effort-Wechsel fehlgeschlagen"));
      expect(screen.getByTestId("effort-chip")).toBeInTheDocument();
    });

    it("keeps the model and effort chips on the documented mono step", () => {
      renderWithEffort();
      // Guarded by shape, not by naming the banned value: any arbitrary pixel
      // size is off the documented ramp, so this catches the next off-ramp step
      // too — and leaves no literal off-ramp size in the file to grep for.
      const arbitraryFontSize = /text-\[\d+px\]/;
      const effortChip = screen.getByTestId("effort-chip").className;
      expect(effortChip).toContain("text-xs");
      expect(effortChip).not.toMatch(arbitraryFontSize);

      const modelChip = screen.getByRole("button", { name: /claude-sonnet-4-6/ }).className;
      expect(modelChip).toContain("text-xs");
      expect(modelChip).not.toMatch(arbitraryFontSize);
    });
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
