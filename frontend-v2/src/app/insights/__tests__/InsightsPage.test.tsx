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
});

describe("InsightsPage", () => {
  it("tells the story in order: hero, heatmap, sources, trend, done, details", async () => {
    const { container } = renderPage();
    await screen.findByTestId("hero-cost");
    const regions = [...container.querySelectorAll("[data-region]")].map((el) => el.getAttribute("data-region"));
    expect(regions).toEqual(["hero", "heatmap", "sources", "trend", "done", "details"]);
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

  it("leaves the heads row out while the launcher is off", async () => {
    renderPage();
    await screen.findByTestId("done-totals");
    expect(screen.queryByTestId("done-heads")).toBeNull();
  });
});
