/** Insights page: one story in a fixed order, days in the browser's zone. */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { mkUsageByDay, mkUsageByWeek } from "@/lib/__tests__/usageFixtures";
import { mkByModel, mkByTask, mkCosts, mkInsights } from "@/components/insights/__tests__/insightsFixtures";

vi.mock("@/components/layout/AppShell", () => ({
  default: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}));

import InsightsPage from "../page";

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <InsightsPage />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.restoreAllMocks();
  vi.spyOn(api.intelligence, "byDay").mockResolvedValue(mkUsageByDay());
  vi.spyOn(api.intelligence, "byWeek").mockResolvedValue(mkUsageByWeek());
  vi.spyOn(api.intelligence, "insights").mockResolvedValue(mkInsights());
  vi.spyOn(api.intelligence, "config").mockResolvedValue({ analysis_window_days: 7 } as never);
  vi.spyOn(api.intelligence, "byModel").mockResolvedValue(mkByModel());
  vi.spyOn(api.intelligence, "byTask").mockResolvedValue(mkByTask());
  vi.spyOn(api.intelligence, "costs").mockResolvedValue(mkCosts());
  vi.spyOn(api.intelligence, "reports").mockResolvedValue([]);
  vi.spyOn(api.usage, "pages").mockResolvedValue({ generated_at: "", weeks: [] });
  vi.spyOn(api.heads, "list").mockRejectedValue(new Error("heads_disabled"));
  vi.spyOn(api.voiceLive, "usageSummary").mockRejectedValue(new Error("API 404"));
});

describe("InsightsPage", () => {
  it("tells the story in order: hero, heatmap, sources, trend, box, done, details", async () => {
    const { container } = renderPage();
    await screen.findByTestId("hero-cost");
    const regions = [...container.querySelectorAll("[data-region]")].map((el) => el.getAttribute("data-region"));
    expect(regions).toEqual(["hero", "heatmap", "sources", "trend", "box", "done", "details"]);
  });

  it("has no tabs and no eyebrow line any more", async () => {
    renderPage();
    await screen.findByTestId("hero-cost");
    expect(screen.queryByRole("tablist")).toBeNull();
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Insights");
  });

  it("asks for days and weeks in the browser's time zone", async () => {
    renderPage();
    const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
    await waitFor(() => expect(api.intelligence.byDay).toHaveBeenCalledWith(371, tz));
    expect(api.intelligence.byWeek).toHaveBeenCalledWith(12, tz);
  });

  it("says so when the usage numbers cannot be loaded, and keeps the rest", async () => {
    vi.spyOn(api.intelligence, "byDay").mockRejectedValue(new Error("boom"));
    renderPage();
    expect(await screen.findByText("Could not load the usage numbers.")).toBeInTheDocument();
    expect(screen.getByTestId("details-models")).toBeInTheDocument();
    expect(await screen.findByTestId("done-totals")).toBeInTheDocument();
  });

  it("shows the live voice's minutes and cost after the local boxes (voice PR 6)", async () => {
    vi.spyOn(api.voiceLive, "usageSummary").mockResolvedValue({
      enabled: true,
      today: { calls: 2, seconds: 720, cost_usd: 0.84 },
      week: { calls: 9, seconds: 2820, cost_usd: 3.1 },
      budget_usd: 5,
      budget_reached: false,
      per_minute_usd: 0.066,
    });
    const { container } = renderPage();
    await screen.findByTestId("voice-usage-today");
    const regions = [...container.querySelectorAll("[data-region]")].map((el) => el.getAttribute("data-region"));
    expect(regions).toEqual(["hero", "heatmap", "sources", "trend", "box", "voice", "done", "details"]);
    expect(screen.getByTestId("voice-usage-today")).toHaveTextContent("Today12 min$0.84");
    expect(screen.getByTestId("voice-usage-week")).toHaveTextContent("7 days47 min$3.10");
    expect(screen.getByTestId("voice-usage-budget")).toHaveTextContent("Daily budget $5.00");
    expect(screen.getByTestId("voice-usage-per-minute")).toHaveTextContent("$0.07 per call minute");
  });

  it("says when today's voice budget is used up", async () => {
    vi.spyOn(api.voiceLive, "usageSummary").mockResolvedValue({
      enabled: true,
      today: { calls: 5, seconds: 3000, cost_usd: 5.02 },
      week: { calls: 5, seconds: 3000, cost_usd: 5.02 },
      budget_usd: 5,
      budget_reached: true,
      per_minute_usd: 0.1,
    });
    renderPage();
    expect(await screen.findByTestId("voice-usage-budget")).toHaveTextContent("used up today");
  });

  it("no voice row while the voice is off and nothing was spent", async () => {
    vi.spyOn(api.voiceLive, "usageSummary").mockResolvedValue({
      enabled: false,
      today: { calls: 0, seconds: 0, cost_usd: 0 },
      week: { calls: 0, seconds: 0, cost_usd: 0 },
      budget_usd: 5,
      budget_reached: false,
      per_minute_usd: null,
    });
    renderPage();
    await screen.findByTestId("done-totals");
    await waitFor(() => expect(api.voiceLive.usageSummary).toHaveBeenCalled());
    expect(screen.queryByTestId("voice-usage-empty")).toBeNull();
    expect(document.querySelector('[data-region="voice"]')).toBeNull();
  });

  it("leaves the heads row out while the launcher is off", async () => {
    renderPage();
    await screen.findByTestId("done-totals");
    expect(screen.queryByTestId("done-heads")).toBeNull();
  });
});
