/**
 * ContextPanel vitest — the numbers are the whole point of this panel, so the
 * arithmetic (free = window − sum, per-row shares, the no-breakdown fallback)
 * gets covered directly, plus the dismissal contract.
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ContextPanel, usedTokensOf } from "./ContextPanel";
import type { UsageEvent } from "@/lib/chatTypes";

function mkUsage(overrides: Partial<UsageEvent> = {}): UsageEvent {
  return {
    kind: "usage",
    uuid: "u1",
    ts: "2026-08-17T10:00:00Z",
    inputTokens: 150_000,
    outputTokens: 1_000,
    model: "claude-opus-5",
    effort: null,
    contextWindow: 1_000_000,
    usedPct: 15.1,
    source: "cli",
    components: {
      input: 10_000,
      cacheRead: 138_000,
      cacheCreation: 2_000,
      output: 1_000,
    },
    ...overrides,
  };
}

function renderPanel(overrides: Partial<React.ComponentProps<typeof ContextPanel>> = {}) {
  const props = {
    usage: mkUsage(),
    pct: 15.1,
    pctSource: "cli" as const,
    onClose: vi.fn(),
    ...overrides,
  };
  return { ...render(<ContextPanel {...props} />), props };
}

describe("usedTokensOf", () => {
  it("sums all four buckets when a breakdown exists", () => {
    expect(usedTokensOf(mkUsage())).toBe(151_000);
  });

  it("falls back to inputTokens when there is no breakdown", () => {
    expect(usedTokensOf(mkUsage({ components: null, inputTokens: 42_000 }))).toBe(42_000);
  });
});

describe("ContextPanel", () => {
  it("names every bucket with its token count", () => {
    renderPanel();
    expect(screen.getByTestId("context-row-input")).toHaveTextContent("Eingabe");
    expect(screen.getByTestId("context-row-input")).toHaveTextContent("10k");
    expect(screen.getByTestId("context-row-cacheRead")).toHaveTextContent("Cache gelesen");
    expect(screen.getByTestId("context-row-cacheCreation")).toHaveTextContent("Cache geschrieben");
    expect(screen.getByTestId("context-row-output")).toHaveTextContent("Ausgabe");
  });

  it("derives Frei as the window minus everything used", () => {
    renderPanel();
    // 1,000,000 − 151,000 = 849,000 → "849k"
    expect(screen.getByTestId("context-row-free")).toHaveTextContent("849k");
  });

  it("shows each row's share of the whole window", () => {
    renderPanel();
    expect(screen.getByTestId("context-row-cacheRead")).toHaveTextContent("13.8%");
    expect(screen.getByTestId("context-row-free")).toHaveTextContent("84.9%");
  });

  it("never lets Frei go negative when the used total overshoots the window", () => {
    renderPanel({
      usage: mkUsage({
        contextWindow: 100_000,
        components: { input: 90_000, cacheRead: 30_000, cacheCreation: 0, output: 0 },
      }),
    });
    expect(screen.getByTestId("context-row-free")).toHaveTextContent("0k");
  });

  it("shows the same percentage the ring shows", () => {
    renderPanel({ pct: 15.1 });
    expect(screen.getByTestId("context-panel-pct")).toHaveTextContent("15%");
  });

  it("falls back to one 'Belegt' row when the backend has no breakdown", () => {
    renderPanel({ usage: mkUsage({ components: null, inputTokens: 200_000 }) });
    expect(screen.getByTestId("context-row-used")).toHaveTextContent("Belegt");
    expect(screen.getByTestId("context-row-free")).toHaveTextContent("800k");
    expect(screen.queryByTestId("context-row-cacheRead")).not.toBeInTheDocument();
  });

  it("omits Frei and the shares when the window is unknown", () => {
    renderPanel({ usage: mkUsage({ contextWindow: null }) });
    expect(screen.queryByTestId("context-row-free")).not.toBeInTheDocument();
    expect(screen.getByText("unbekannt")).toBeInTheDocument();
    expect(screen.getByTestId("context-row-input")).not.toHaveTextContent("%");
  });

  it("drops empty buckets instead of listing zero rows", () => {
    renderPanel({
      usage: mkUsage({ components: { input: 5_000, cacheRead: 0, cacheCreation: 0, output: 0 } }),
    });
    expect(screen.getByTestId("context-row-input")).toBeInTheDocument();
    expect(screen.queryByTestId("context-row-cacheRead")).not.toBeInTheDocument();
  });

  it("names the provenance of the figures", () => {
    renderPanel({ pctSource: "cli" });
    expect(screen.getByTestId("context-panel-source")).toHaveTextContent("CLI");
  });

  it("says so when the figures are an estimate", () => {
    renderPanel({ pctSource: "estimate" });
    expect(screen.getByTestId("context-panel-source")).toHaveTextContent("Schätzung");
  });

  it("closes on Escape", async () => {
    const onClose = vi.fn();
    const user = userEvent.setup();
    renderPanel({ onClose });
    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalled();
  });

  it("closes on a click outside the panel", async () => {
    const onClose = vi.fn();
    const user = userEvent.setup();
    renderPanel({ onClose });
    await user.click(document.body);
    expect(onClose).toHaveBeenCalled();
  });

  it("stays open on a click inside the panel", async () => {
    const onClose = vi.fn();
    const user = userEvent.setup();
    renderPanel({ onClose });
    await user.click(screen.getByText("Kontext"));
    expect(onClose).not.toHaveBeenCalled();
  });

  it("offers an explicit close control for touch", async () => {
    const onClose = vi.fn();
    const user = userEvent.setup();
    renderPanel({ onClose });
    await user.click(screen.getByRole("button", { name: "Schliessen" }));
    expect(onClose).toHaveBeenCalled();
  });
});
