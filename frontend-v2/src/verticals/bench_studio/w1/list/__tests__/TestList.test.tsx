/**
 * Bench W1 list (P7, package D): sections Läuft · Braucht Blick · Fertig from
 * GET /bench/tests, every row a link to its result page, header "Modell
 * testen" into the test sheet, filters model/device/harness/class, the empty,
 * no-hit, loading and error states. Data = the contract fixtures.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { benchW1Api, benchW1Keys } from "../../api";
import { contestantsFixture, runtimeSummaryFixture, testListFixture } from "../../fixtures";
import type { BenchTestList } from "../../types";
import { benchResultHref, benchTestHref, type BenchRuntimeSummary } from "@/lib/benchLinks";
import { BENCH_RUNTIME_SUMMARY_KEY } from "@/app/runtimes/benchSummary";
import { TestList } from "../TestList";

function renderList() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <TestList />
    </QueryClientProvider>,
  );
}

const EMPTY: BenchTestList = {
  items: [],
  counts: { running: 0, needs_look: 0, done: 0 },
  total: 0,
  last_at: null,
};

describe("TestList — sections and rows (P7)", () => {
  beforeEach(() => {
    vi.spyOn(benchW1Api.tests, "list").mockResolvedValue(testListFixture);
    vi.spyOn(benchW1Api, "contestants").mockResolvedValue(contestantsFixture);
  });
  afterEach(() => vi.restoreAllMocks());

  it("shows the three sections in order with their counts", async () => {
    renderList();
    const sections = await screen.findAllByTestId(/^bench-section-(running|needs_look|done)$/);
    expect(sections.map((s) => s.getAttribute("data-testid"))).toEqual([
      "bench-section-running",
      "bench-section-needs_look",
      "bench-section-done",
    ]);
    expect(within(sections[0]).getByRole("heading")).toHaveTextContent("Running");
    expect(within(sections[1]).getByRole("heading")).toHaveTextContent("Needs a look");
    expect(within(sections[2]).getByRole("heading")).toHaveTextContent("Done");
    expect(within(sections[0]).getByTestId("bench-section-count")).toHaveTextContent("2");
    expect(within(sections[2]).getByTestId("bench-section-count")).toHaveTextContent("1");
  });

  it("every row is a link to its result page", async () => {
    renderList();
    const rows = await screen.findAllByTestId("bench-test-row");
    expect(rows).toHaveLength(testListFixture.items.length);
    for (const [i, item] of testListFixture.items.entries()) {
      expect(rows[i]).toHaveAttribute("href", benchResultHref(item.id));
    }
  });

  it("header: title, count + last date, and 'Test a model' into the sheet", async () => {
    renderList();
    expect(await screen.findByRole("heading", { level: 1 })).toHaveTextContent("Benchmark");
    expect(screen.getByTestId("bench-list-lead")).toHaveTextContent(/^5 tests · last /);
    const cta = screen.getByTestId("bench-list-test");
    expect(cta).toHaveTextContent("Test a model");
    expect(cta).toHaveAttribute("href", benchTestHref({ trigger: "list" }));
  });

  it("states in human words: running progress, waiting reason, missing numbers, private truncation, done", async () => {
    renderList();
    const rows = await screen.findAllByTestId("bench-test-row");
    // running 1/3
    expect(rows[0]).toHaveTextContent("Running · 1/3");
    // waiting: shared word + the wait sentence with the start time
    expect(rows[1]).toHaveTextContent("Waiting");
    expect(rows[1]).toHaveTextContent(/Model only just started — starts at \d{1,2}:\d\d( [AP]M)?/);
    // agent run with numbers missing
    expect(rows[2]).toHaveTextContent("Done – numbers missing");
    expect(rows[2]).toHaveTextContent("omp");
    // amber: truncated runs are named (operator-private list)
    expect(rows[3]).toHaveTextContent("Postable with notes");
    expect(rows[3]).toHaveTextContent("1 of 3 runs hit the token limit");
    // green done: a quiet word, no pill
    expect(rows[4]).toHaveTextContent("Done");
    expect(within(rows[4]).queryByTestId("bench-row-pill")).toBeNull();
    // model + harness tag
    expect(rows[0]).toHaveTextContent("DeepSeek-V4.1");
    expect(rows[0]).toHaveTextContent("Raw API");
  });

  it("desktop pick hint offers to test the running model", async () => {
    renderList();
    const hint = await screen.findByTestId("bench-pick-hint");
    expect(within(hint).getByRole("link")).toHaveAttribute("href", benchTestHref({ trigger: "list" }));
  });

  it("links to the prompt library and toggles the archive", async () => {
    const list = vi.mocked(benchW1Api.tests.list);
    renderList();
    await screen.findAllByTestId("bench-test-row");
    expect(screen.getByTestId("bench-list-library")).toHaveAttribute("href", "/bench?view=library");
    await userEvent.click(screen.getByTestId("bench-list-archive"));
    await waitFor(() =>
      expect(list).toHaveBeenCalledWith(expect.objectContaining({ includeArchived: true })),
    );
    expect(screen.getByTestId("bench-list-archive")).toHaveTextContent("Hide archive");
  });
});

describe("TestList — filters", () => {
  beforeEach(() => {
    vi.spyOn(benchW1Api, "contestants").mockResolvedValue(contestantsFixture);
  });
  afterEach(() => vi.restoreAllMocks());

  it("a harness filter asks the server and shows the active chip", async () => {
    const list = vi.spyOn(benchW1Api.tests, "list").mockResolvedValue(testListFixture);
    renderList();
    await screen.findAllByTestId("bench-test-row");
    await userEvent.selectOptions(screen.getByLabelText("Harness"), "omp");
    await waitFor(() =>
      expect(list).toHaveBeenCalledWith(expect.objectContaining({ harness: "omp" })),
    );
    expect(screen.getByTestId("bench-filter-harness")).toHaveTextContent("Harness: omp");
  });

  it("model options come from the tests, device options from the devices", async () => {
    vi.spyOn(benchW1Api.tests, "list").mockResolvedValue(testListFixture);
    renderList();
    await screen.findAllByTestId("bench-test-row");
    const model = screen.getByLabelText("Model") as HTMLSelectElement;
    expect([...model.options].map((o) => o.value)).toEqual(["", "DeepSeek-V4.1"]);
    await waitFor(() => {
      const device = screen.getByLabelText("Device") as HTMLSelectElement;
      expect([...device.options].map((o) => o.value)).toContain("DGX Spark ×2");
    });
  });

  it("no hit: names the filter and offers a reset", async () => {
    const list = vi.spyOn(benchW1Api.tests, "list").mockImplementation(async (q = {}) =>
      q.harness ? EMPTY : testListFixture,
    );
    renderList();
    await screen.findAllByTestId("bench-test-row");
    await userEvent.selectOptions(screen.getByLabelText("Harness"), "grok-build");
    expect(await screen.findByText("No test with Grok Build")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Reset filters" }));
    await waitFor(() => expect(screen.getAllByTestId("bench-test-row")).toHaveLength(5));
    expect((screen.getByLabelText("Harness") as HTMLSelectElement).value).toBe("");
    expect(list).toHaveBeenCalledWith(expect.objectContaining({ harness: "grok-build" }));
  });
});

describe("TestList — empty, loading, error", () => {
  afterEach(() => vi.restoreAllMocks());

  it("no test yet: explains the way in, no header button", async () => {
    vi.spyOn(benchW1Api.tests, "list").mockResolvedValue(EMPTY);
    vi.spyOn(benchW1Api, "contestants").mockResolvedValue(contestantsFixture);
    renderList();
    expect(await screen.findByText("No test yet", { selector: "h2" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Go to Runtimes/ })).toHaveAttribute("href", "/runtimes");
    expect(screen.queryByTestId("bench-list-test")).toBeNull();
    expect(screen.queryByTestId("bench-filter-harness")).toBeNull();
  });

  it("loading shows a quiet placeholder", () => {
    vi.spyOn(benchW1Api.tests, "list").mockReturnValue(new Promise(() => {}));
    vi.spyOn(benchW1Api, "contestants").mockResolvedValue(contestantsFixture);
    renderList();
    expect(screen.getByTestId("bench-list-loading")).toHaveTextContent("Loading …");
  });

  it("error: a sentence and a retry", async () => {
    const list = vi.spyOn(benchW1Api.tests, "list").mockRejectedValueOnce(new Error("API 500: boom"));
    list.mockResolvedValue(testListFixture);
    vi.spyOn(benchW1Api, "contestants").mockResolvedValue(contestantsFixture);
    renderList();
    expect(await screen.findByText("The list cannot be loaded right now.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findAllByTestId("bench-test-row")).toHaveLength(5);
  });
});

describe("core door ↔ vertical (contract)", () => {
  it("the Runtimes page caches the summary under the vertical's key", () => {
    expect([...BENCH_RUNTIME_SUMMARY_KEY]).toEqual([...benchW1Keys.runtimeSummary()]);
  });

  it("the core summary type is a structural subset of the vertical's", () => {
    const rows: BenchRuntimeSummary[] = runtimeSummaryFixture;
    expect(rows).toHaveLength(runtimeSummaryFixture.length);
  });
});
