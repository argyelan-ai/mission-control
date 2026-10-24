/**
 * Bench W1 P1 (package D): "Testen" on the Runtimes page.
 *
 *   - live card (ActionBar): Testen from GET /bench/runtime-summary — only the
 *     most recently started card is primary (K11); the switch trigger steps
 *     down to secondary there; waiting/fresh/running/blocked say why
 *   - "Letzter Test: …" links to that result
 *   - a box without a model (asleep / free): "Testen geht, sobald ein Modell läuft"
 *   - cloud rows: a text button "Testen" where the summary says testable
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ActionBar } from "../ActionBar";
import { AsleepBox } from "../AsleepBox";
import { CloudUsage } from "../../CloudUsage";
import { api } from "@/lib/api";
import { benchResultHref, benchTestHref, type BenchRuntimeSummary } from "@/lib/benchLinks";
import type { Host, Runtime } from "@/lib/types";

const fetchSummary = vi.hoisted(() => vi.fn());
vi.mock("@/lib/benchLinks", async (orig) => ({
  ...(await orig<typeof import("@/lib/benchLinks")>()),
  fetchBenchRuntimeSummary: fetchSummary,
}));

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

function summary(over: Partial<BenchRuntimeSummary> = {}): BenchRuntimeSummary {
  return {
    runtime_id: "rt-1",
    testable: true,
    reason: null,
    primary: true,
    last_test: null,
    running_test_id: null,
    ...over,
  };
}

function bar(bench: BenchRuntimeSummary | null, variant: "normal" | "trouble" = "normal") {
  return wrap(
    <ActionBar
      hostId="spark"
      hostName="spark"
      servingName="DeepSeek-V4.1"
      runtimeId="rt-1"
      variant={variant}
      onOpenCockpit={() => {}}
      bench={bench}
    />,
  );
}

describe("ActionBar — Testen (P1)", () => {
  beforeEach(() => {
    vi.spyOn(api.hosts, "recipes").mockResolvedValue([]);
  });
  afterEach(() => vi.restoreAllMocks());

  it("primary card: Testen is the one accent button, the switch steps down", async () => {
    bar(summary());
    const test = await screen.findByTestId("bench-test");
    expect(test).toHaveTextContent("Test");
    expect(test).toHaveAttribute("href", benchTestHref({ runtimeId: "rt-1", trigger: "runtimes" }));
    expect(test).toHaveAttribute("data-primary", "true");
    expect(screen.getByTestId("recipe-dropdown-trigger")).toHaveAttribute("data-variant", "secondary");
  });

  it("other live cards: Testen secondary, the switch keeps its primary", async () => {
    bar(summary({ primary: false }));
    expect(await screen.findByTestId("bench-test")).toHaveAttribute("data-primary", "false");
    expect(screen.getByTestId("recipe-dropdown-trigger")).toHaveAttribute("data-variant", "primary");
  });

  it("fresh model: the button names the start time, the note says why", async () => {
    bar(summary({ reason: { code: "model_fresh", params: { until: "2026-10-24T09:45:00Z" } } }));
    expect(await screen.findByTestId("bench-test")).toHaveTextContent(/^Test from \d{1,2}:\d\d( [AP]M)?$/);
    expect(screen.getByTestId("bench-note")).toHaveTextContent(
      "MC does not measure the first 15 min: the model is still warming up.",
    );
  });

  it("busy box: the test waits", async () => {
    bar(summary({ reason: { code: "box_busy", params: {} } }));
    expect(await screen.findByTestId("bench-test")).toHaveTextContent("Test (waits)");
    expect(screen.getByTestId("bench-note")).toHaveTextContent("A test waits until the box is free.");
  });

  it("a running test: the button opens it", async () => {
    bar(summary({ running_test_id: "t-9" }));
    const test = await screen.findByTestId("bench-test");
    expect(test).toHaveTextContent("Test running");
    expect(test).toHaveAttribute("href", benchResultHref("t-9"));
  });

  it("not testable: no link, the blocker in words", async () => {
    bar(summary({ testable: false, reason: { code: "protocol_mismatch", params: {} } }));
    const test = await screen.findByTestId("bench-test");
    expect(test.tagName).toBe("BUTTON");
    expect(test).toHaveAttribute("aria-disabled", "true");
    expect(test).not.toHaveAttribute("href");
    expect(screen.getByTestId("bench-note")).toHaveTextContent("Protocol does not fit");
  });

  it("the last test links to its result", async () => {
    bar(
      summary({
        last_test: { id: "a2", title: "Pagoda Garden", created_at: "2026-10-22T19:40:00Z" },
      }),
    );
    const last = await screen.findByTestId("bench-last-test");
    expect(last).toHaveTextContent(/^Last test: .+ · Pagoda Garden$/);
    expect(last).toHaveAttribute("href", benchResultHref("a2"));
  });

  it("trouble card and no summary: no Testen", async () => {
    const { unmount } = bar(summary(), "trouble");
    await screen.findByTestId("restart-now");
    expect(screen.queryByTestId("bench-test")).toBeNull();
    unmount();
    bar(null);
    await screen.findByTestId("recipe-dropdown-trigger");
    expect(screen.queryByTestId("bench-test")).toBeNull();
    expect(screen.getByTestId("recipe-dropdown-trigger")).toHaveAttribute("data-variant", "primary");
  });
});

describe("a box without a model", () => {
  afterEach(() => vi.restoreAllMocks());

  it("asleep box: Testen disabled with the reason", () => {
    const host = { id: "h-p", slug: "porsche", display_name: "Porsche", power_managed: true } as unknown as Host;
    wrap(<AsleepBox host={host} onOpenCockpit={() => {}} />);
    const test = screen.getByTestId("bench-test-no-model");
    expect(test).toHaveAttribute("aria-disabled", "true");
    expect(screen.getByText("Testing works once a model runs")).toBeInTheDocument();
  });
});

describe("CloudUsage — Testen per cloud row (W1.12)", () => {
  afterEach(() => vi.restoreAllMocks());

  const rt = (id: string, model: string): Runtime =>
    ({ id, slug: id, display_name: model, model_identifier: model, runtime_type: "cloud" }) as unknown as Runtime;

  it("testable rows get a Testen link, the others none", async () => {
    vi.spyOn(api.runtimes.db, "agents").mockImplementation(async (slug: string) =>
      slug === "c-1"
        ? ({ count: 1, agents: [{ id: "a1", name: "Rex" }] } as never)
        : ({ count: 0, agents: [] } as never),
    );
    fetchSummary.mockResolvedValue([
      summary({ runtime_id: "c-1", primary: false }),
      summary({ runtime_id: "c-2", testable: false, reason: { code: "no_api_key", params: {} }, primary: false }),
    ]);
    wrap(<CloudUsage runtimes={[rt("c-1", "kimi-k3"), rt("c-2", "glm-5.2")]} onOpen={() => {}} />);
    const row = await screen.findByTestId("cloud-usage-row-c-1");
    const link = await within(row).findByTestId("bench-test-cloud");
    expect(link).toHaveTextContent("Test");
    expect(link).toHaveAttribute("href", benchTestHref({ runtimeId: "c-1", trigger: "runtimes" }));
    // expand the unbound rows: no Testen where the summary says not testable
    (await screen.findByTestId("cloud-usage-collapse-toggle")).click();
    const row2 = await screen.findByTestId("cloud-usage-row-c-2");
    await waitFor(() => expect(within(row2).queryByTestId("bench-test-cloud")).toBeNull());
  });
});
