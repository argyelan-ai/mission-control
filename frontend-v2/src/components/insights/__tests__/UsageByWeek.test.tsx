/** Insights → "Usage by week": the E0 baseline and page usage in MC. */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { api } from "@/lib/api";
import { mkPageViews, mkTotals, mkUsageByWeek } from "@/lib/__tests__/usageFixtures";
import { UsageByWeek, UsageByWeekView } from "../UsageByWeek";

function wrap(ui: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

describe("UsageByWeekView", () => {
  it("lists six weeks newest first with cost and local share", () => {
    wrap(<UsageByWeekView data={mkUsageByWeek()} pages={mkPageViews()} />);
    const rows = screen.getAllByTestId(/^usage-week-/);
    expect(rows.map((r) => r.dataset.testid)).toEqual([
      "usage-week-2026-W40", "usage-week-2026-W39", "usage-week-2026-W38",
      "usage-week-2026-W37", "usage-week-2026-W36", "usage-week-2026-W35",
    ]);
    expect(within(rows[0]).getByText("Week 40 · so far")).toBeInTheDocument();
    expect(within(rows[0]).getByText("$120")).toBeInTheDocument();
    expect(within(rows[0]).getByText("20%")).toBeInTheDocument();
    expect(within(rows[1]).getByText("Week 39")).toBeInTheDocument();
    expect(within(rows[1]).getByText("$355")).toBeInTheDocument();
  });

  it("scales the bar to the busiest week", () => {
    wrap(<UsageByWeekView data={mkUsageByWeek()} />);
    const bar = (week: string) => screen.getByTestId(`usage-week-${week}`).querySelector("span[aria-hidden] > span") as HTMLElement;
    expect(bar("2026-W38").style.width).toBe("100%"); // $480, the busiest
    expect(bar("2026-W40").style.width).toBe("25%"); // $120
  });

  it("leaves out the local share of a week without tokens (K3)", () => {
    const data = mkUsageByWeek();
    data.weeks[0].totals = mkTotals(0, 0);
    wrap(<UsageByWeekView data={data} />);
    expect(within(screen.getByTestId("usage-week-2026-W35")).queryByText(/%/)).toBeNull();
  });

  it("shows this week's top three sources", () => {
    wrap(<UsageByWeekView data={mkUsageByWeek()} />);
    expect(screen.getByText("You, interactive")).toBeInTheDocument();
    expect(screen.getByText("Lead agent")).toBeInTheDocument();
    expect(screen.getByText("Agents · cli-bridge")).toBeInTheDocument();
    expect(screen.queryByText("Heads local")).toBeNull(); // 4th by cost
  });

  it("shows the top five pages of this week", () => {
    wrap(<UsageByWeekView data={mkUsageByWeek()} pages={mkPageViews()} />);
    const pages = within(screen.getByTestId("usage-pages")).getAllByRole("listitem");
    expect(pages).toHaveLength(5);
    expect(pages[0]).toHaveTextContent("/tasks");
    expect(pages[0]).toHaveTextContent("19");
  });

  it("says so when no pages were counted yet", () => {
    wrap(<UsageByWeekView data={mkUsageByWeek()} pages={{ generated_at: "", weeks: [] }} />);
    expect(screen.getByText("No page views counted yet.")).toBeInTheDocument();
  });

  it("says so when there is no usage at all", () => {
    const data = mkUsageByWeek();
    data.weeks.forEach((w) => { w.totals = mkTotals(0, 0); w.sources = []; });
    wrap(<UsageByWeekView data={data} />);
    expect(screen.getByText("No usage recorded yet.")).toBeInTheDocument();
    expect(screen.queryAllByTestId(/^usage-week-/)).toHaveLength(0);
  });
});

describe("UsageByWeek", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("loads six weeks and this week's pages", async () => {
    const weeks = vi.spyOn(api.intelligence, "byWeek").mockResolvedValue(mkUsageByWeek());
    const pages = vi.spyOn(api.usage, "pages").mockResolvedValue(mkPageViews());
    wrap(<UsageByWeek />);
    expect(await screen.findByTestId("usage-by-week")).toBeInTheDocument();
    expect(weeks).toHaveBeenCalledWith(6);
    expect(pages).toHaveBeenCalledWith(1);
  });
});
