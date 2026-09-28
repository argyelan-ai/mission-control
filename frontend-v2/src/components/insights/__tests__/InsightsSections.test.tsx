/** Insights sections: hero, heatmap, sources, trend, done, details. */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, within, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { api } from "@/lib/api";
import { mkDay, mkSource, mkTotals, mkUsageByDay, mkUsageByWeek, mkUsageWeek } from "@/lib/__tests__/usageFixtures";
import { InsightsHero } from "../InsightsHero";
import { UsageHeatmap } from "../UsageHeatmap";
import { SourceSplit } from "../SourceSplit";
import { LocalShareTrend } from "../LocalShareTrend";
import { DoneSummary, headsInWindow } from "../DoneSummary";
import { InsightsDetails, cacheHitRate } from "../InsightsDetails";
import { mkByModel, mkByTask, mkCosts, mkHeadRun, mkInsights } from "./insightsFixtures";

function wrap(ui: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

describe("InsightsHero", () => {
  const days = {
    ...mkUsageByDay(),
    days: [
      mkDay("2026-09-21", 10, 0, 100), mkDay("2026-09-22", 10, 0, 100), mkDay("2026-09-23", 10, 0, 100),
      mkDay("2026-09-28", 10, 0, 50), mkDay("2026-09-29", 10, 0, 100), mkDay("2026-09-30", 10, 0, 0),
    ],
  };

  it("leads with this week's list-price equivalent and says it is one", () => {
    wrap(<InsightsHero days={days} weeks={mkUsageByWeek()} />);
    expect(screen.getByTestId("hero-cost")).toHaveTextContent("$150");
    expect(screen.getByText(/list-price equivalent — flat-rate plans are not billed per token/)).toBeInTheDocument();
  });

  it("compares with the same weekdays of last week", () => {
    wrap(<InsightsHero days={days} weeks={mkUsageByWeek()} />);
    expect(screen.getByTestId("hero-change")).toHaveTextContent("50% less than the same days last week");
  });

  it("shows the local share of this week with last week's next to it", () => {
    wrap(<InsightsHero days={days} weeks={mkUsageByWeek()} />);
    const local = screen.getByTestId("hero-local");
    expect(local).toHaveTextContent("20%");
    expect(local).toHaveTextContent("ran locally");
    expect(local).toHaveTextContent("last week 15%");
    expect(within(local).getByRole("meter")).toHaveAttribute("aria-valuenow", "20");
  });

  it("draws the line over finished weeks only", () => {
    wrap(<InsightsHero days={days} weeks={mkUsageByWeek()} />);
    const points = screen.getByTestId("hero-sparkline").querySelector("polyline")!.getAttribute("points")!;
    expect(points.split(" ")).toHaveLength(5); // 6 weeks, the running one left out
  });

  it("says so when nothing was recorded yet", () => {
    const empty = mkUsageByWeek();
    empty.weeks = empty.weeks.map((w) => ({ ...w, totals: mkTotals(0, 0) }));
    wrap(<InsightsHero days={{ ...days, days: [mkDay("2026-09-30", 0, 0, 0)] }} weeks={empty} />);
    expect(screen.getByText("No usage recorded yet.")).toBeInTheDocument();
    expect(screen.queryByTestId("hero-cost")).toBeNull();
  });
});

describe("UsageHeatmap", () => {
  it("draws 26 weeks for the phone and 52 for the desktop", () => {
    wrap(<UsageHeatmap data={mkUsageByDay()} />);
    expect(screen.getByTestId("heatmap-grid-26").querySelectorAll("rect[data-date]")).toHaveLength(25 * 7 + 3);
    expect(screen.getByTestId("heatmap-grid-52").querySelectorAll("rect[data-date]")).toHaveLength(51 * 7 + 3);
  });

  it("starts on today and steps through the days with 44 px arrows", () => {
    wrap(<UsageHeatmap data={mkUsageByDay()} />);
    const row = screen.getByTestId("heatmap-day");
    expect(row).toHaveTextContent("Wednesday, September 30");
    expect(screen.getByRole("button", { name: "Next day" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Previous day" }));
    expect(row).toHaveTextContent("Tuesday, September 29");
  });

  it("selects a day by tapping its cell and shows its numbers", () => {
    const data = mkUsageByDay();
    data.days[data.days.length - 2] = mkDay("2026-09-29", 1_200_000, 240_000, 214);
    wrap(<UsageHeatmap data={data} />);
    const cell = screen.getByTestId("heatmap-grid-26").querySelector('rect[data-date="2026-09-29"]')!;
    fireEvent.click(cell);
    const ring = within(screen.getByTestId("heatmap-grid-26") as unknown as HTMLElement).getByTestId("heatmap-selected");
    expect([ring.getAttribute("x"), ring.getAttribute("y")]).toEqual([
      String(Number(cell.getAttribute("x")) - 2),
      String(Number(cell.getAttribute("y")) - 2),
    ]);
    const row = screen.getByTestId("heatmap-day");
    expect(row).toHaveTextContent("1.2M tokens");
    expect(row).toHaveTextContent("$214");
    expect(row).toHaveTextContent("20% local");
    expect(row).toHaveTextContent("Mostly You, interactive");
  });

  it("switches the colour to the local tokens", () => {
    const days = [mkDay("2026-09-28", 1000, 0), mkDay("2026-09-29", 10, 10), mkDay("2026-09-30", 0, 0)];
    wrap(<UsageHeatmap data={{ ...mkUsageByDay(), days }} />);
    const level = (date: string) =>
      screen.getByTestId("heatmap-grid-26").querySelector(`rect[data-date="${date}"]`)!.getAttribute("data-level");
    expect(level("2026-09-28")).toBe("4");
    fireEvent.click(screen.getByRole("radio", { name: "Local" }));
    expect(level("2026-09-28")).toBe("0");
    expect(level("2026-09-29")).toBe("4");
  });

  it("names what counts as a token and where the day ends", () => {
    wrap(<UsageHeatmap data={mkUsageByDay()} />);
    expect(screen.getByText("Tokens = input + output, without cache reads. A day ends at midnight in Europe/Zurich.")).toBeInTheDocument();
  });
});

describe("SourceSplit", () => {
  it("lists the four biggest sources with share and folds the rest", () => {
    const week = mkUsageWeek({
      sources: [
        mkSource("operator", 60, 1), mkSource("lead", 20, 1), mkSource("heads:local", 10, 1),
        mkSource("agents:cli-bridge", 6, 1), mkSource("agents:grok", 3, 1), mkSource("unattributed", 1, 1),
      ],
    });
    wrap(<SourceSplit week={week} />);
    expect(screen.getByTestId("source-operator")).toHaveTextContent("You, interactive$60.0060%");
    expect(screen.getByTestId("source-agents:cli-bridge")).toHaveTextContent("Agents · cli-bridge");
    expect(screen.getByTestId("source-rest")).toHaveTextContent("Other (2)$4.004.0%");
    expect(screen.getByTestId("sources-bar").children).toHaveLength(5);
    expect(screen.getByText("this week · list price")).toBeInTheDocument();
  });

  it("says so when the week has no cost yet", () => {
    wrap(<SourceSplit week={mkUsageWeek({ sources: [] })} />);
    expect(screen.getByText("No cost recorded this week yet.")).toBeInTheDocument();
  });
});

describe("LocalShareTrend", () => {
  it("draws one bar per week, the running week in the accent", () => {
    const weeks = mkUsageByWeek().weeks;
    wrap(<LocalShareTrend weeks={weeks} />);
    const bars = screen.getByTestId("trend-bars").querySelectorAll("li");
    expect(bars).toHaveLength(6);
    expect(bars[5]).toHaveAttribute("aria-label", "W40: 20%");
    const height = (i: number) => (bars[i].firstChild as HTMLElement).style.height;
    expect(height(5)).toBe("100%"); // the best week fills the chart
    expect(height(1)).toBe("2%"); // 0 % still shows a sliver
  });
});

describe("DoneSummary", () => {
  const now = Date.parse("2026-09-30T12:00:00Z");

  it("counts tasks and lists only agents that did something", () => {
    wrap(<DoneSummary insights={mkInsights()} windowDays={7} heads={null} />);
    expect(screen.getByTestId("done-totals")).toHaveTextContent("14tasks done2failed");
    expect(screen.getByTestId("done-agent-Coder")).toHaveTextContent("6 done · 1 failed · Ø 9 min");
    expect(screen.getByTestId("done-agent-Reviewer")).toHaveTextContent("5 done · Ø 4 min");
    expect(screen.queryByTestId("done-agent-Idle")).toBeNull();
    expect(screen.queryByTestId("done-heads")).toBeNull();
  });

  it("counts head runs of the window only", () => {
    const runs = [
      mkHeadRun("passed", "2026-09-29T10:00:00Z"), mkHeadRun("failed", "2026-09-28T10:00:00Z"),
      mkHeadRun("running", "2026-09-30T10:00:00Z"), mkHeadRun("passed", "2026-09-01T10:00:00Z"),
    ];
    expect(headsInWindow(runs, 7, now)).toEqual({ runs: 3, passed: 1, failed: 1 });
  });

  it("writes anomalies as sentences in the UI language, not the backend text", () => {
    wrap(<DoneSummary insights={mkInsights()} windowDays={7} heads={null} />);
    const notes = screen.getByTestId("done-notes");
    expect(notes).not.toHaveTextContent("backend text");
    expect(notes).toHaveTextContent("far longer than usual");
  });

  it("names why tasks failed", () => {
    wrap(<DoneSummary insights={mkInsights()} windowDays={7} heads={null} />);
    expect(screen.getByTestId("done-patterns")).toHaveTextContent("timeout: 1 · merge_conflict: 1");
  });

  it("waits for the first analysis", () => {
    wrap(<DoneSummary insights={mkInsights({ analyzed_at: null })} windowDays={7} heads={null} />);
    expect(screen.getByText("The first analysis runs a few minutes after the backend starts.")).toBeInTheDocument();
  });
});

describe("InsightsDetails", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api.intelligence, "byModel").mockResolvedValue(mkByModel());
    vi.spyOn(api.intelligence, "byTask").mockResolvedValue(mkByTask());
    vi.spyOn(api.intelligence, "costs").mockImplementation(async (_d, s) => mkCosts(!!s));
    vi.spyOn(api.intelligence, "reports").mockResolvedValue([
      { id: "r1", created_at: "2026-09-29T08:00:00Z", content: "**Bold** finding", title: "Report" } as never,
    ]);
    vi.spyOn(api.usage, "pages").mockResolvedValue({
      generated_at: "", weeks: [{ week: "2026-W40", week_start: "2026-09-28", total: 3, dropped: 0, routes: { "/tasks": 2, "/": 1 } }],
    });
  });

  it("computes the cache hit rate", () => {
    expect(cacheHitRate(mkByModel())).toBeCloseTo(38_400_000 / (38_400_000 + 3_010_000));
    expect(cacheHitRate([])).toBeNull();
  });

  it("keeps every old block reachable, folded, with a summary", async () => {
    wrap(<InsightsDetails />);
    for (const id of ["models", "tasks", "agents", "sessions", "pages", "reports"]) {
      expect(screen.getByTestId(`details-${id}`)).toBeInTheDocument();
    }
    await waitFor(() => expect(screen.getByTestId("details-models")).toHaveTextContent("cache hits 92.7%"));
    expect(screen.queryByText("cloud-large")).toBeNull(); // folded
  });

  it("loads the session list only when it is opened", async () => {
    wrap(<InsightsDetails />);
    await waitFor(() => expect(api.intelligence.costs).toHaveBeenCalledWith(30, false));
    expect(api.intelligence.costs).not.toHaveBeenCalledWith(30, true);
    fireEvent.click(within(screen.getByTestId("details-sessions")).getByRole("button"));
    await waitFor(() => expect(api.intelligence.costs).toHaveBeenCalledWith(30, true));
    expect(await screen.findByText("task:00000000")).toBeInTheDocument();
  });

  it("renders AI reports as Markdown, not raw asterisks", async () => {
    wrap(<InsightsDetails />);
    fireEvent.click(within(screen.getByTestId("details-reports")).getByRole("button"));
    const bold = await screen.findByText("Bold");
    expect(bold.tagName).toBe("STRONG");
  });

  it("applies the time range to the details", async () => {
    wrap(<InsightsDetails />);
    fireEvent.click(screen.getByRole("radio", { name: "7 days" }));
    await waitFor(() => expect(api.intelligence.byModel).toHaveBeenCalledWith(7));
  });
});
