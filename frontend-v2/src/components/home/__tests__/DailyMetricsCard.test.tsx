/** Home → "Measured today": the daily metrics digest (M1–M5) in MC. */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { api } from "@/lib/api";
import { STATUS_TEXT } from "@/lib/colors";
import { mkDaily } from "@/lib/__tests__/usageFixtures";
import { DailyMetricsCard, DailyMetricsCardView } from "../DailyMetricsCard";

function wrap(ui: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

const metric = (key: string) => screen.getByTestId(`daily-metric-${key}`);

describe("DailyMetricsCardView", () => {
  it("shows the five digest numbers with their labels", () => {
    wrap(<DailyMetricsCardView data={mkDaily()} />);
    expect(within(metric("stale")).getByText("2")).toBeInTheDocument();
    expect(within(metric("stale")).getByText("Cards idle over 4 h")).toBeInTheDocument();
    expect(within(metric("reviews")).getByText("2 / 5")).toBeInTheDocument();
    expect(within(metric("dispatch")).getByText("0 / 1")).toBeInTheDocument();
    expect(within(metric("hand")).getByText("4")).toBeInTheDocument();
    expect(within(metric("week")).getByText("$120")).toBeInTheDocument();
    expect(within(metric("week")).getByText("Week 40 · list price")).toBeInTheDocument();
    expect(within(metric("week")).getByText("20% local")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "All numbers in Insights" })).toHaveAttribute("href", "/insights");
  });

  it("colours idle cards only when there are any", () => {
    const { unmount } = wrap(<DailyMetricsCardView data={mkDaily()} />);
    expect(within(metric("stale")).getByText("2")).toHaveStyle({ color: STATUS_TEXT.warning });
    unmount();
    wrap(<DailyMetricsCardView data={mkDaily({ stale_cards: [] })} />);
    expect(within(metric("stale")).getByText("0")).not.toHaveStyle({ color: STATUS_TEXT.warning });
  });

  it("leaves the week out when there is no usage or the query failed", () => {
    const { unmount } = wrap(<DailyMetricsCardView data={mkDaily({ usage_week: null })} />);
    expect(screen.queryByTestId("daily-metric-week")).toBeNull();
    unmount();
    wrap(<DailyMetricsCardView data={mkDaily({ usage_week: { error: true } })} />);
    expect(screen.queryByTestId("daily-metric-week")).toBeNull();
  });
});

describe("DailyMetricsCard", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("loads the numbers from the backend", async () => {
    const spy = vi.spyOn(api.system, "dailyMetrics").mockResolvedValue(mkDaily());
    wrap(<DailyMetricsCard />);
    expect(await screen.findByTestId("daily-metrics-card")).toBeInTheDocument();
    expect(spy).toHaveBeenCalledTimes(1);
  });

  it("renders nothing when the numbers are unavailable", async () => {
    vi.spyOn(api.system, "dailyMetrics").mockRejectedValue(new Error("down"));
    const { container } = wrap(<DailyMetricsCard />);
    await new Promise((r) => setTimeout(r, 20));
    expect(container).toBeEmptyDOMElement();
  });
});
