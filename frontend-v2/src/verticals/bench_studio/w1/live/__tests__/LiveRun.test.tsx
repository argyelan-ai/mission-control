// Package B — the live run (P4). One card per contestant, run i/n, time,
// tokens so far, decode for class A only, wait reason, class B after class
// A ("startet nach Lauf 3"), Stop. The result page (package C) polls the
// detail and passes the fresh record in.
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { BenchTestDetail } from "../../types";
import { testRunningFixture } from "../../fixtures";

const api = vi.hoisted(() => ({ stop: vi.fn() }));

vi.mock("../../api", async (importOriginal) => {
  const orig = await importOriginal<typeof import("../../api")>();
  return {
    ...orig,
    benchW1Api: { ...orig.benchW1Api, tests: { ...orig.benchW1Api.tests, stop: api.stop } },
  };
});

import { LiveRun } from "../LiveRun";

function clone<T>(v: T): T {
  return JSON.parse(JSON.stringify(v)) as T;
}

function renderLive(test: BenchTestDetail, onStopped = vi.fn()) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <LiveRun test={test} onStopped={onStopped} />
    </QueryClientProvider>,
  );
  return { onStopped, region: screen.getByTestId("bench-live-run") };
}

beforeEach(() => {
  api.stop.mockReset();
});

describe("LiveRun — running", () => {
  it("shows the state line and one card per contestant", () => {
    const { region } = renderLive(clone(testRunningFixture));
    expect(region).toHaveAttribute("data-test-id", testRunningFixture.id);
    expect(within(region).getByTestId("bench-live-status")).toHaveTextContent("Running · Run 2 of 3");
    expect(within(region).getByText("The agent run starts afterwards.")).toBeInTheDocument();
    expect(within(region).getAllByTestId("bench-live-card")).toHaveLength(2);
  });

  it("class A card: run lines with time, tokens so far and decode", () => {
    const { region } = renderLive(clone(testRunningFixture));
    const [cardA] = within(region).getAllByTestId("bench-live-card");
    expect(within(cardA).getByText("Raw API · Default")).toBeInTheDocument();
    expect(within(cardA).getByText("Model only")).toBeInTheDocument();
    const runs = within(cardA).getAllByTestId("bench-live-runline");
    expect(runs).toHaveLength(3);
    expect(runs[0]).toHaveTextContent("Run 1");
    expect(runs[0]).toHaveTextContent("1 min 6 s");
    expect(runs[0]).toHaveTextContent("6,607 tok");
    expect(runs[1]).toHaveTextContent("0:41");
    expect(runs[1]).toHaveTextContent("3,100 tok");
    expect(runs[1]).toHaveTextContent("101 tok/s");
    expect(runs[2]).toHaveTextContent("waiting");
  });

  it("class B card: after class A, why, no tempo", () => {
    const { region } = renderLive(clone(testRunningFixture));
    expect(within(region).getByText("Then: Agent builds")).toBeInTheDocument();
    const cardB = within(region).getAllByTestId("bench-live-card")[1];
    expect(within(cardB).getByText("omp (local agent)")).toBeInTheDocument();
    expect(within(cardB).getByText("single run")).toBeInTheDocument();
    expect(within(cardB).getByText("Starts after run 3")).toBeInTheDocument();
    expect(within(cardB).getByText("The box should be quiet while measuring")).toBeInTheDocument();
    expect(cardB.textContent).not.toMatch(/tok\/s/);
  });

  it("a cut-off run is named privately in the live card", () => {
    const t = clone(testRunningFixture);
    const r = t.blocks[0].contestants[0].runs[1];
    Object.assign(r, { state: "failed", phase: "done", truncated: true, finish_reason: "length" });
    t.blocks[0].contestants[0].runs[2].state = "running";
    t.blocks[0].contestants[0].runs[2].phase = "generating";
    t.blocks[0].contestants[0].runs[2].elapsed_ms = 22000;
    t.blocks[0].contestants[0].runs[2].tokens_out = 1900;
    const { region } = renderLive(t);
    expect(within(region).getByTestId("bench-live-status")).toHaveTextContent("Run 3 of 3, 1 failure");
    expect(within(region).getByText("Failed attempts are counted too.")).toBeInTheDocument();
    const runs = within(region).getAllByTestId("bench-live-runline");
    expect(runs[1]).toHaveTextContent("cut off");
    expect(runs[1]).toHaveTextContent("max_tokens reached");
  });
});

describe("LiveRun — waiting", () => {
  it("says why the test waits", () => {
    const t = clone(testRunningFixture);
    t.state = "waiting";
    t.state_reason = { code: "box_busy", params: {} };
    for (const r of t.blocks[0].contestants[0].runs) {
      Object.assign(r, { state: "waiting", phase: "queued", elapsed_ms: null, tokens_out: null, decode_tok_s: null });
    }
    const { region } = renderLive(t);
    expect(within(region).getByTestId("bench-live-status")).toHaveTextContent(
      "Waiting · Box busy — starts as soon as it is free",
    );
    expect(within(region).getByText(/After 10 min it starts anyway/)).toBeInTheDocument();
  });
});

describe("LiveRun — stop", () => {
  it("stops after a second tap and hands the record back", async () => {
    const stopped = { ...clone(testRunningFixture), state: "aborted" as const, allowed: [] };
    api.stop.mockResolvedValue(stopped);
    const user = userEvent.setup();
    const { onStopped, region } = renderLive(clone(testRunningFixture));
    const stop = within(region).getByRole("button", { name: "Stop" });
    await user.click(stop);
    expect(api.stop).not.toHaveBeenCalled();
    await user.click(within(region).getByRole("button", { name: "Really stop?" }));
    await waitFor(() => expect(api.stop).toHaveBeenCalledWith(testRunningFixture.id));
    await waitFor(() => expect(onStopped).toHaveBeenCalledWith(stopped));
  });

  it("shows no Stop when the record does not allow it", () => {
    const t = clone(testRunningFixture);
    t.allowed = [];
    renderLive(t);
    expect(screen.queryByRole("button", { name: "Stop" })).not.toBeInTheDocument();
  });

  it("a failed stop is said in words", async () => {
    api.stop.mockRejectedValue(new Error("API 500: nope"));
    const user = userEvent.setup();
    const { onStopped, region } = renderLive(clone(testRunningFixture));
    await user.click(within(region).getByRole("button", { name: "Stop" }));
    await user.click(within(region).getByRole("button", { name: "Really stop?" }));
    expect(await within(region).findByRole("alert")).toHaveTextContent("Stopping failed.");
    expect(onStopped).not.toHaveBeenCalled();
  });
});
