/**
 * ContextPanel vitest — the numbers are the whole point of this panel, so the
 * arithmetic (free = window − sum, per-row shares, the no-breakdown fallback)
 * gets covered directly, plus the dismissal contract.
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ContextPanel, usedTokensOf, cacheHitRateOf } from "./ContextPanel";
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

describe("cacheHitRateOf", () => {
  it("computes cached / (cached + uncached input) for the latest turn", () => {
    // 138,000 / (138,000 + 10,000) — the GLM-via-omp case this exists for.
    expect(cacheHitRateOf(mkUsage().components)).toBeCloseTo(0.932432, 5);
  });

  it("returns null when the harness has no breakdown at all", () => {
    expect(cacheHitRateOf(null)).toBeNull();
    expect(cacheHitRateOf(undefined)).toBeNull();
  });

  it("returns null rather than a fake 0% when both buckets are empty", () => {
    expect(
      cacheHitRateOf({ input: 0, cacheRead: 0, cacheCreation: 500, output: 10 }),
    ).toBeNull();
  });
});

describe("ContextPanel", () => {
  it("names every bucket with its token count", () => {
    renderPanel();
    expect(screen.getByTestId("context-row-input")).toHaveTextContent("Input");
    expect(screen.getByTestId("context-row-input")).toHaveTextContent("10k");
    expect(screen.getByTestId("context-row-cacheRead")).toHaveTextContent("Cache read");
    expect(screen.getByTestId("context-row-cacheCreation")).toHaveTextContent("Cache written");
    expect(screen.getByTestId("context-row-output")).toHaveTextContent("Output");
  });

  it("derives Free as the window minus everything used", () => {
    renderPanel();
    // 1,000,000 − 151,000 = 849,000 → "849k"
    expect(screen.getByTestId("context-row-free")).toHaveTextContent("849k");
  });

  it("shows each row's share of the whole window", () => {
    renderPanel();
    expect(screen.getByTestId("context-row-cacheRead")).toHaveTextContent("13.8%");
    expect(screen.getByTestId("context-row-free")).toHaveTextContent("84.9%");
  });

  it("never lets Free go negative when the used total overshoots the window", () => {
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

  it("falls back to one 'Used' row when the backend has no breakdown", () => {
    renderPanel({ usage: mkUsage({ components: null, inputTokens: 200_000 }) });
    expect(screen.getByTestId("context-row-used")).toHaveTextContent("Used");
    expect(screen.getByTestId("context-row-free")).toHaveTextContent("800k");
    expect(screen.queryByTestId("context-row-cacheRead")).not.toBeInTheDocument();
  });

  it("shows the omp fill figure (usedTokens) as Used, never the cumulative input — live 14.09.2026: fill 92,799 of 500k while cumulative input was 11.8M (task 156f57c7)", () => {
    renderPanel({
      usage: mkUsage({
        inputTokens: 92_799,
        contextWindow: 500_000,
        usedPct: 18.6,
        components: null,
      }),
      pct: 18.6,
    });
    expect(screen.getByTestId("context-row-used")).toHaveTextContent("93k");
    expect(screen.getByTestId("context-row-free")).toHaveTextContent("407k");
    expect(screen.getByTestId("context-panel-pct")).toHaveTextContent("19%");
  });

  it("omits Free and the shares when the window is unknown", () => {
    renderPanel({ usage: mkUsage({ contextWindow: null }) });
    expect(screen.queryByTestId("context-row-free")).not.toBeInTheDocument();
    expect(screen.getByText("unknown")).toBeInTheDocument();
    expect(screen.getByTestId("context-row-input")).not.toHaveTextContent("%");
  });

  it("drops empty buckets instead of listing zero rows", () => {
    renderPanel({
      usage: mkUsage({ components: { input: 5_000, cacheRead: 0, cacheCreation: 0, output: 0 } }),
    });
    expect(screen.getByTestId("context-row-input")).toBeInTheDocument();
    expect(screen.queryByTestId("context-row-cacheRead")).not.toBeInTheDocument();
  });

  it("shows the cache hit rate when the harness reports a breakdown", () => {
    renderPanel();
    // 138,000 / 148,000 ≈ 93.2% → rounds to "93%".
    expect(screen.getByTestId("context-panel-cache-rate")).toHaveTextContent("93%");
  });

  it("hides the cache hit rate row rather than show a fake 0%", () => {
    renderPanel({ usage: mkUsage({ components: null, inputTokens: 200_000, cacheHitRate: null }) });
    expect(screen.queryByTestId("context-panel-cache-rate")).not.toBeInTheDocument();
  });

  it("shows the cache hit rate for an omp ACP agent (no components breakdown, but the backend still computed a rate from this turn's real numbers — the context panel used to show only Used/Free here)", () => {
    renderPanel({
      usage: mkUsage({
        components: null,
        inputTokens: 92_799,
        contextWindow: 500_000,
        usedPct: 18.6,
        cacheHitRate: 0.9,
      }),
      pct: 18.6,
    });
    // Window-breakdown segments stay a single "Used" row (no invented
    // per-turn/window-wide split), but the hit rate is still honest.
    expect(screen.getByTestId("context-row-used")).toBeInTheDocument();
    expect(screen.getByTestId("context-panel-cache-rate")).toHaveTextContent("90%");
  });

  it("names the provenance of the figures", () => {
    renderPanel({ pctSource: "cli" });
    expect(screen.getByTestId("context-panel-source")).toHaveTextContent("CLI");
  });

  it("says so when the figures are an estimate", () => {
    renderPanel({ pctSource: "estimate" });
    expect(screen.getByTestId("context-panel-source")).toHaveTextContent("estimate");
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
    await user.click(screen.getByText("Context · this chat"));
    expect(onClose).not.toHaveBeenCalled();
  });

  it("offers an explicit close control for touch", async () => {
    const onClose = vi.fn();
    const user = userEvent.setup();
    renderPanel({ onClose });
    await user.click(screen.getByRole("button", { name: "Close" }));
    expect(onClose).toHaveBeenCalled();
  });
});

describe("session cost (ACP usage_update.cost, chat package P1)", () => {
  it("shows the reported cost in the active locale", () => {
    renderPanel({ usage: mkUsage({ sessionCost: { amount: 0.012855, currency: "USD" } }) });
    expect(screen.getByText("Session cost")).toBeInTheDocument();
    expect(screen.getByTestId("context-panel-cost")).toHaveTextContent("$0.0129");
  });

  it("shows NO cost row when the harness reported none (local model)", () => {
    renderPanel({ usage: mkUsage({ sessionCost: null }) });
    expect(screen.queryByTestId("context-panel-cost")).toBeNull();
    renderPanel({ usage: mkUsage({ sessionCost: { amount: 0, currency: "USD" } }) });
    expect(screen.queryByTestId("context-panel-cost")).toBeNull();
  });
});

describe("formatSessionCost", () => {
  it("keeps four decimals below one unit, two above, never fakes zero", async () => {
    const { formatSessionCost } = await import("./ContextPanel");
    expect(formatSessionCost({ amount: 0.012855, currency: "USD" }, "en")).toBe("$0.0129");
    expect(formatSessionCost({ amount: 12.5, currency: "USD" }, "en")).toBe("$12.50");
    expect(formatSessionCost({ amount: 1.5, currency: "EUR" }, "de")?.replace(/\s/g, " ")).toBe("1,50 €");
    expect(formatSessionCost({ amount: 0, currency: "USD" }, "en")).toBeNull();
    expect(formatSessionCost({ amount: Number.NaN, currency: "USD" }, "en")).toBeNull();
    expect(formatSessionCost(undefined, "en")).toBeNull();
    expect(formatSessionCost({ amount: 2, currency: "NOT-A-CODE" }, "en")).toBe("2.00 NOT-A-CODE");
  });
});

// Round-4 user test (t3 NICE-1): on the iPhone the panel said only
// "Kontext 5 %" — that it is THIS chat (the agent page shows the work
// session, 2 %) stood only in the ring's tooltip, and a phone has no hover.
describe("ContextPanel names its session", () => {
  it("says it is this chat and why the agent page may differ, visibly", () => {
    renderPanel();
    const panel = screen.getByTestId("context-panel");
    expect(panel).toHaveTextContent(/this chat/i);
    expect(panel).toHaveTextContent(/agent page/i);
    expect(panel.querySelector("[title]")).toBeNull();
  });
});

