import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { BenchTestDetail } from "../../types";
import {
  testAbortedTruncatedFixture,
  testDoneGreenFixture,
  testDoneTruncatedFixture,
  testNeedsLookRedFixture,
  testRunningFixture,
} from "../../fixtures";

const push = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push, replace: vi.fn(), back: vi.fn() }),
  usePathname: () => "/bench/r/x",
  useSearchParams: () => new URLSearchParams(),
}));

vi.mock("@/lib/api", () => ({
  api: { files: { contentUrl: (root: string, sub: string) => `/files/${root}/${sub}` } },
  BASE_URL: "",
  getToken: () => "test-token",
  request: vi.fn(),
}));

vi.mock("@/verticals/bench_studio/w1/api", async () => {
  const actual = await vi.importActual<typeof import("../../api")>("@/verticals/bench_studio/w1/api");
  return {
    ...actual,
    LIVE_POLL_MS: 25,
    benchW1Api: {
      tests: { get: vi.fn(), rerun: vi.fn(), harvest: vi.fn(), stop: vi.fn(), seen: vi.fn().mockResolvedValue({ resolved: 0 }) },
      kit: { get: vi.fn(), downloadToken: vi.fn(), fileUrl: vi.fn() },
    },
  };
});

vi.mock("@/verticals/bench_studio/api", () => ({
  benchApi: {
    challenges: {
      rerender: vi.fn().mockResolvedValue({ ok: true }),
      recompose: vi.fn().mockResolvedValue({ ok: true }),
      archive: vi.fn().mockResolvedValue({}),
      remove: vi.fn().mockResolvedValue(undefined),
    },
    entries: { viewToken: vi.fn().mockResolvedValue({ token: "vt", expires_in: 1800 }) },
    sharedSubpath: (p: string) => p.replace(/^\/shared-deliverables\//, ""),
    entryViewUrl: (c: string, e: string, t: string) => `/view/${c}/${e}?token=${t}`,
  },
}));

vi.mock("@/lib/notify", () => ({
  notify: { success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() },
}));

vi.mock("@/components/task/FilePreview", () => ({
  FilePreview: ({ fileUrl }: { fileUrl: string }) => <div data-testid="video-preview">{fileUrl}</div>,
}));

vi.mock("@/verticals/bench_studio/w1/live/LiveRun", () => ({
  LiveRun: ({ test }: { test: BenchTestDetail }) => (
    <div data-testid="live-run-mock">
      {test.id}:{test.state}
    </div>
  ),
}));

vi.mock("@/verticals/bench_studio/w1/list/TestList", () => ({
  TestList: () => <div data-testid="test-list-mock" />,
}));

vi.mock("@/verticals/bench_studio/w1/kit/XKitSheet", () => ({
  XKitSheet: ({ open, testId }: { open: boolean; testId: string }) =>
    open ? <div data-testid="xkit-open">{testId}</div> : null,
}));

import { benchW1Api } from "@/verticals/bench_studio/w1/api";
import { benchApi } from "@/verticals/bench_studio/api";
import { request } from "@/lib/api";
import { benchResultHref, benchTestHref } from "@/lib/benchLinks";
import { resetTruncationAckForTests } from "../truncationAck";
import { ResultView } from "../ResultPage";

const get = vi.mocked(benchW1Api.tests.get);

function renderView(test: BenchTestDetail) {
  get.mockResolvedValue(test);
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const utils = render(
    <QueryClientProvider client={qc}>
      <ResultView testId={test.id} />
    </QueryClientProvider>,
  );
  return { ...utils, qc };
}

async function openMenu(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole("button", { name: "More actions" }));
  return screen.getByRole("menu");
}

beforeEach(() => {
  vi.clearAllMocks();
  resetTruncationAckForTests();
});

describe("ResultPage — live", () => {
  it("renders the live run while running and polls the record", async () => {
    renderView(testRunningFixture);
    expect(await screen.findByTestId("live-run-mock")).toHaveTextContent(
      `${testRunningFixture.id}:running`,
    );
    // No verdict, no X-Kit, no numbers while it runs.
    expect(screen.queryByRole("button", { name: /X-Kit/ })).toBeNull();
    expect(screen.queryByTestId("result-numbers")).toBeNull();
    // ONE state line: the live run (package B) brings its own (K3).
    expect(screen.queryByTestId("result-state")).toBeNull();
    await waitFor(() => expect(get.mock.calls.length).toBeGreaterThanOrEqual(2));
  });

  it("stops polling once the test is done", async () => {
    renderView(testDoneGreenFixture);
    await screen.findByTestId("result-verdict");
    const calls = get.mock.calls.length;
    await new Promise((r) => setTimeout(r, 120));
    expect(get.mock.calls.length).toBe(calls);
  });
});

describe("ResultPage — green class A result", () => {
  it("names the served model exactly once (K3) in the verdict line", async () => {
    renderView(testDoneGreenFixture);
    const verdict = await screen.findByTestId("result-verdict");
    expect(verdict).toHaveTextContent("DeepSeek-V4.1-Flash-1017");
    expect(verdict).toHaveTextContent("3 runs");
    expect(verdict).toHaveTextContent("1:02–1:11");
    expect(verdict).toHaveTextContent("~6.6k tokens out");
    expect(verdict).toHaveTextContent("page loads");
    // Receipt is closed: the name stands nowhere else.
    expect(screen.getAllByText(/DeepSeek-V4\.1-Flash-1017/)).toHaveLength(1);
  });

  it("shows the gate light and median + range per number", async () => {
    renderView(testDoneGreenFixture);
    expect(await screen.findByTestId("result-gate")).toHaveTextContent("Ready to post");
    const list = screen.getByTestId("numbers-list");
    expect(within(list).getByText("Compute time")).toBeInTheDocument();
    expect(list).toHaveTextContent("1:06");
    expect(list).toHaveTextContent("median of 3 · 1:02–1:11");
    expect(list).toHaveTextContent("6.4k–6.9k");
    expect(list).toHaveTextContent("of which 2.1k thinking");
    expect(list).toHaveTextContent("101 tok/s");
    expect(list).toHaveTextContent("first token after 0.4 s");
    // D3: local + small hypothetical API price, never a bare $ as the cost.
    expect(list).toHaveTextContent("local · costs nothing");
    expect(list).toHaveTextContent("API price would be ≈ $0.04");
    expect(within(list).getByText("loads")).toBeInTheDocument();
  });

  it("has one primary X-Kit button that opens the kit sheet", async () => {
    const user = userEvent.setup();
    renderView(testDoneGreenFixture);
    const kit = await screen.findByRole("button", { name: "X-Kit" });
    expect(kit).toHaveAttribute("data-primary", "true");
    expect(document.querySelectorAll('[data-primary="true"]')).toHaveLength(1);
    await user.click(kit);
    expect(screen.getByTestId("xkit-open")).toHaveTextContent(testDoneGreenFixture.id);
  });

  it("'Run again' opens the test sheet as a rerun", async () => {
    const user = userEvent.setup();
    renderView(testDoneGreenFixture);
    await user.click(await screen.findByRole("button", { name: "Run again" }));
    expect(push).toHaveBeenCalledWith(
      benchTestHref({ rerunOf: testDoneGreenFixture.id, trigger: "rerun" }),
    );
  });

  it("maps allowed[] into the ⋯ menu (rerender, recompose, archive, delete)", async () => {
    const user = userEvent.setup();
    renderView(testDoneGreenFixture);
    const menu = await openMenu(user);
    for (const label of ["Re-record video", "Rebuild video", "Archive", "Delete", "Open page of run 1"]) {
      expect(within(menu).getByRole("menuitem", { name: label })).toBeInTheDocument();
    }
    await user.click(within(menu).getByRole("menuitem", { name: "Rebuild video" }));
    await waitFor(() =>
      expect(benchApi.challenges.recompose).toHaveBeenCalledWith(testDoneGreenFixture.id),
    );
  });

  it("asks before deleting, then goes back to the list", async () => {
    const user = userEvent.setup();
    renderView(testDoneGreenFixture);
    const menu = await openMenu(user);
    await user.click(within(menu).getByRole("menuitem", { name: "Delete" }));
    expect(benchApi.challenges.remove).not.toHaveBeenCalled();
    await user.click(await screen.findByRole("button", { name: "Delete test" }));
    await waitFor(() => expect(benchApi.challenges.remove).toHaveBeenCalledWith(testDoneGreenFixture.id));
    await waitFor(() => expect(push).toHaveBeenCalledWith("/bench"));
  });

  it("shows the class A receipt with unknowns spelled out", async () => {
    const user = userEvent.setup();
    renderView(testDoneGreenFixture);
    await user.click(await screen.findByRole("button", { name: /Receipt · all settings/ }));
    const receipt = screen.getByTestId("receipt-A");
    expect(receipt).toHaveTextContent("Model only · same prompt");
    expect(receipt).toHaveTextContent("sha d6e0ea9");
    expect(receipt).toHaveTextContent("temp 0.7 · max 16,384");
    expect(receipt).toHaveTextContent("3 · video = run 1 · 0 failures");
    expect(receipt).toHaveTextContent("DGX Spark ×2 · local · GPU mode eco");
    expect(receipt).toHaveTextContent("unknownhand-made runtime, no recipe");
    expect(receipt).toHaveTextContent("blocked · 0 attempts");
    expect(receipt).toHaveTextContent("v1 · 22/10/2026");
  });

  it("renders overlay actions from a safe endpoint only", async () => {
    const user = userEvent.setup();
    vi.mocked(request).mockResolvedValue({});
    renderView({
      ...testDoneGreenFixture,
      overlay_actions: [
        {
          id: "publish",
          label: "Publish episode",
          style: "primary",
          method: "POST",
          endpoint: "/api/v1/overlay/publish",
          confirm: null,
          disabled: false,
          disabled_reason: null,
          busy: false,
        },
        {
          id: "evil",
          label: "Evil",
          style: "default",
          method: "POST",
          endpoint: "//evil.example/x",
          confirm: null,
          disabled: false,
          disabled_reason: null,
          busy: false,
        },
      ],
    });
    const menu = await openMenu(user);
    expect(within(menu).queryByRole("menuitem", { name: "Evil" })).toBeNull();
    await user.click(within(menu).getByRole("menuitem", { name: "Publish episode" }));
    await waitFor(() =>
      expect(request).toHaveBeenCalledWith("/api/v1/overlay/publish", { method: "POST" }),
    );
  });
});

describe("ResultPage — red agent result (v0)", () => {
  it("makes 'Load numbers' the primary action and calls harvest", async () => {
    const user = userEvent.setup();
    vi.mocked(benchW1Api.tests.harvest).mockResolvedValue(testNeedsLookRedFixture);
    renderView(testNeedsLookRedFixture);
    expect(await screen.findByTestId("result-gate")).toHaveTextContent("Not ready to post");
    expect(screen.getByTestId("result-gate")).toHaveTextContent(
      "A headline number is missing: tokens out",
    );
    const primary = screen.getByRole("button", { name: "Load numbers" });
    expect(primary).toHaveAttribute("data-primary", "true");
    expect(screen.queryByRole("button", { name: "X-Kit" })).toBeNull();
    await user.click(primary);
    await waitFor(() =>
      expect(benchW1Api.tests.harvest).toHaveBeenCalledWith(testNeedsLookRedFixture.id),
    );
  });

  it("shows 'not measured: reason' only for the headline and no speed row (class B)", async () => {
    renderView(testNeedsLookRedFixture);
    const list = await screen.findByTestId("numbers-list");
    expect(within(list).getByText("Work time")).toBeInTheDocument();
    expect(list).toHaveTextContent("92 min");
    expect(list).toHaveTextContent("no waiting");
    expect(list).toHaveTextContent("not measured: numbers not loaded yet");
    expect(within(list).queryByText("Speed")).toBeNull();
    expect(list).toHaveTextContent("omp (local agent)");
    expect(list).toHaveTextContent("omp session: high");
  });

  it("names the model once in the numbers when there is no verdict", async () => {
    renderView(testNeedsLookRedFixture);
    await screen.findByTestId("numbers-list");
    expect(screen.queryByTestId("result-verdict")).toBeNull();
    // Phone list + desktop table both render in jsdom; the list names it once.
    expect(within(screen.getByTestId("numbers-list")).getAllByText(/DeepSeek-V4\.1-Flash-1017/)).toHaveLength(1);
  });

  it("class A receipt says the budget is the whole context and shows the time limit (25.10.)", async () => {
    const user = userEvent.setup();
    const block = testDoneGreenFixture.blocks[0];
    const first = block.contestants[0];
    renderView({
      ...testDoneGreenFixture,
      blocks: [
        {
          ...block,
          contestants: [
            { ...first, receipt: first.receipt && { ...first.receipt, max_tokens: 126976, max_tokens_rule: "context", time_limit_min: 120 } },
            ...block.contestants.slice(1),
          ],
        },
        ...testDoneGreenFixture.blocks.slice(1),
      ],
    });
    await user.click(await screen.findByRole("button", { name: /Receipt · all settings/ }));
    const receipt = screen.getByTestId("receipt-A");
    expect(receipt).toHaveTextContent("max 126,976 (whole context)");
    expect(receipt).toHaveTextContent("Time limit120 min");
  });

  it("shows the class B receipt with v0 unknowns spelled out", async () => {
    const user = userEvent.setup();
    renderView(testNeedsLookRedFixture);
    await user.click(await screen.findByRole("button", { name: /Receipt · all settings/ }));
    const receipt = screen.getByTestId("receipt-B");
    expect(receipt).toHaveTextContent("Agent builds · one brief");
    expect(receipt).toHaveTextContent("sha 7777777 · tools allowed");
    expect(receipt).toHaveTextContent("Time limit");
    expect(receipt).toHaveTextContent("not recorded");
    expect(receipt).toHaveTextContent("version unknown");
    expect(receipt).toHaveTextContent("observed, not set by the test");
    expect(receipt).toHaveTextContent("open — recorded before the block");
    expect(receipt).toHaveTextContent("v0");
    expect(receipt).toHaveTextContent("numbers not loaded yet");
  });

  it("keeps actions the server did not allow out of the menu", async () => {
    const user = userEvent.setup();
    renderView(testNeedsLookRedFixture);
    const menu = await openMenu(user);
    expect(within(menu).queryByRole("menuitem", { name: "Re-record video" })).toBeNull();
    expect(within(menu).queryByRole("menuitem", { name: "Rebuild video" })).toBeNull();
    expect(within(menu).getByRole("menuitem", { name: "Archive" })).toBeInTheDocument();
  });

  it("still opens the X-Kit as a preview from the ⋯ menu", async () => {
    const user = userEvent.setup();
    renderView(testNeedsLookRedFixture);
    const menu = await openMenu(user);
    await user.click(within(menu).getByRole("menuitem", { name: "X-Kit (preview)" }));
    expect(screen.getByTestId("xkit-open")).toHaveTextContent(testNeedsLookRedFixture.id);
  });
});

describe("ResultPage — private truncation warning", () => {
  it("warns above the X-Kit button and does not repeat it in the gate line", async () => {
    renderView(testDoneTruncatedFixture);
    const warning = await screen.findByTestId("truncation-warning");
    expect(warning).toHaveTextContent("1 of 3 runs hit the token limit");
    expect(warning).toHaveTextContent("Run 2 was stopped at 16,384 tokens: the answer is unfinished");
    expect(warning).toHaveTextContent("Only you see this");
    const kit = screen.getByRole("button", { name: "X-Kit" });
    // DOM order: warning first, then the button.
    expect(warning.compareDocumentPosition(kit) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getAllByText(/runs hit the token limit/)).toHaveLength(1);
  });

  it("the larger budget is the one primary until 'Continue anyway' (S1)", async () => {
    renderView(testDoneTruncatedFixture);
    const rerun = await screen.findByRole("button", { name: "Re-run with 32,768 tokens" });
    expect(rerun).toHaveAttribute("data-primary", "true");
    expect(screen.getByRole("button", { name: "X-Kit" })).not.toHaveAttribute("data-primary");
    // no plain "Run again" at the same budget next to it
    expect(screen.queryByRole("button", { name: "Run again" })).toBeNull();
    expect(document.querySelectorAll('[data-primary="true"]')).toHaveLength(1);
  });

  it("all runs thought until the limit: aborted, but warned with the larger budget (MUST-1)", async () => {
    renderView(testAbortedTruncatedFixture);
    const warning = await screen.findByTestId("truncation-warning");
    expect(warning).toHaveTextContent("thought until the limit of 16,384 tokens and wrote no page");
    const rerun = within(warning).getByRole("button", { name: "Re-run with 32,768 tokens" });
    expect(rerun).toHaveAttribute("data-primary", "true");
    expect(screen.queryByRole("button", { name: "Run again" })).toBeNull();
  });

  it("a local run that filled the whole context is warned without a larger-budget re-run (25.10.)", async () => {
    renderView({
      ...testDoneTruncatedFixture,
      allowed: testDoneTruncatedFixture.allowed.filter((a) => a !== "rerun_larger"),
      truncation: {
        ...testDoneTruncatedFixture.truncation!,
        max_tokens: 126976,
        suggested_max_tokens: 126976,
        larger_budget: false,
      },
    });
    const warning = await screen.findByTestId("truncation-warning");
    expect(warning).toHaveTextContent("filled the model's whole context (126,976 tokens)");
    expect(warning).toHaveTextContent("no room for a larger budget");
    expect(within(warning).queryByRole("button", { name: /Re-run with/ })).toBeNull();
    expect(within(warning).getByRole("button", { name: "Continue anyway" })).toBeInTheDocument();
  });

  it("says when the video run itself was cut", async () => {
    renderView({
      ...testDoneTruncatedFixture,
      truncation: { ...testDoneTruncatedFixture.truncation!, video_run_cut: true },
    });
    expect(await screen.findByTestId("truncation-video")).toHaveTextContent("it shows an unfinished page");
  });

  it("re-runs with a larger budget and opens the new result", async () => {
    const user = userEvent.setup();
    vi.mocked(benchW1Api.tests.rerun).mockResolvedValue({ ...testRunningFixture, id: "new-test" });
    renderView(testDoneTruncatedFixture);
    await user.click(await screen.findByRole("button", { name: "Re-run with 32,768 tokens" }));
    await waitFor(() =>
      expect(benchW1Api.tests.rerun).toHaveBeenCalledWith(testDoneTruncatedFixture.id, "larger"),
    );
    await waitFor(() => expect(push).toHaveBeenCalledWith(benchResultHref("new-test")));
  });

  it("a paid model confirms the price on the spot and keeps the larger budget (D4)", async () => {
    const user = userEvent.setup();
    vi.mocked(benchW1Api.tests.rerun)
      .mockRejectedValueOnce(
        new Error('API 409: {"detail":{"code":"bench_cost_confirmation","step":1,"usd":0.2,"cap_usd":1.0}}'),
      )
      .mockResolvedValueOnce({ ...testRunningFixture, id: "new-test" });
    renderView(testDoneTruncatedFixture);
    await user.click(await screen.findByRole("button", { name: "Re-run with 32,768 tokens" }));
    const ask = await screen.findByRole("button", { name: "Re-run with 32,768 tokens · $0.20 — OK?" });
    // no detour through the sheet — that would lose the larger budget
    expect(push).not.toHaveBeenCalled();
    await user.click(ask);
    await waitFor(() =>
      expect(benchW1Api.tests.rerun).toHaveBeenLastCalledWith(testDoneTruncatedFixture.id, "larger", {
        cost_confirmed_usd: 0.2,
        confirm_over_cap: false,
      }),
    );
    await waitFor(() => expect(push).toHaveBeenCalledWith(benchResultHref("new-test")));
  });

  it("'Continue anyway' puts the warning away", async () => {
    const user = userEvent.setup();
    renderView(testDoneTruncatedFixture);
    await user.click(await screen.findByRole("button", { name: "Continue anyway" }));
    expect(screen.queryByTestId("truncation-warning")).toBeNull();
    expect(screen.getByRole("button", { name: "X-Kit" })).toBeInTheDocument();
  });
});

// 25.10. (operator, Windows desktop): the warning sat alone in the right
// column, top right, away from the test it describes. It belongs to the
// state of the test: main column, right under the head. The rail says what
// to do and what it was (K12 properties): X-Kit / Run again, checks and
// receipt of a finished test; start, contestant and Run again of a stopped
// one. A live test has no rail.
describe("ResultPage — where the parts sit on the desktop", () => {
  const region = (el: Element, name: string) => el.closest(`[data-region="${name}"]`);

  it("stopped, all runs cut: the warning sits in the main column under the state line; start and contestant in the rail", async () => {
    renderView(testAbortedTruncatedFixture);
    const warning = await screen.findByTestId("truncation-warning");
    expect(region(warning, "result-main")).not.toBeNull();
    expect(region(warning, "result-side")).toBeNull();
    const state = screen.getByTestId("result-state");
    expect(region(state, "result-main")).not.toBeNull();
    expect(state.compareDocumentPosition(warning) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(warning.compareDocumentPosition(screen.getByTestId("result-no-video")) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    const facts = document.querySelector('[data-region="result-facts"]')!;
    expect(region(facts, "result-side")).not.toBeNull();
    // In the narrow rail a long model name moves under its label whole.
    for (const row of Array.from(facts.querySelectorAll("dl > div"))) {
      expect(row.className).toMatch(/(^|\s)flex-wrap(\s|$)/);
    }
    expect(screen.getByTestId("bench-result-page").getAttribute("data-columns")).toBe("main-side");
  });

  it("stopped without a cut: 'Run again' heads the rail, above start and contestant", async () => {
    renderView({ ...testAbortedTruncatedFixture, truncation: null });
    const rerun = await screen.findByRole("button", { name: "Run again" });
    const side = region(rerun, "result-side")!;
    expect(side).not.toBeNull();
    const facts = side.querySelector('[data-region="result-facts"]')!;
    expect(rerun.compareDocumentPosition(facts) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("finished with a cut run: warning in the main column, kit, checks and receipt in the rail", async () => {
    renderView(testDoneTruncatedFixture);
    const warning = await screen.findByTestId("truncation-warning");
    expect(region(warning, "result-main")).not.toBeNull();
    expect(region(warning, "result-side")).toBeNull();
    expect(region(screen.getByRole("button", { name: "X-Kit" }), "result-side")).not.toBeNull();
    expect(region(screen.getByRole("region", { name: "Check" }), "result-side")).not.toBeNull();
    expect(screen.getByTestId("bench-result-page").getAttribute("data-columns")).toBe("main-side");
  });

  it("finished green: the video stage leads the main column; X-Kit, 'Run again', checks and receipt in the rail", async () => {
    renderView(testDoneGreenFixture);
    const kit = await screen.findByRole("button", { name: "X-Kit" });
    const side = region(kit, "result-side")!;
    expect(side).not.toBeNull();
    expect(within(side as HTMLElement).getByRole("button", { name: "Run again" })).toBeInTheDocument();
    expect(side.querySelector('[data-region="result-checks"]')).not.toBeNull();
    expect(side.querySelector('[data-region="result-receipt"]')).not.toBeNull();
    const stage = screen.getByTestId("result-video-stage");
    expect(region(stage, "result-main")).not.toBeNull();
    // The stage keeps its 16:9 place while the bytes load: nothing jumps.
    expect(stage.className).toMatch(/(^|\s)aspect-video(\s|$)/);
  });

  it("stopped: the main column says run by run what happened — time, thought vs. written, outcome in words", async () => {
    renderView(testAbortedTruncatedFixture);
    const runs = await screen.findByTestId("result-runs");
    expect(runs.closest('[data-region="result-main"]')).not.toBeNull();
    expect(screen.getByTestId("result-no-video").compareDocumentPosition(runs) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    const rows = within(runs).getAllByTestId("result-run-row");
    expect(rows).toHaveLength(3);
    expect(rows[0]).toHaveTextContent("Run 1");
    expect(rows[0]).toHaveTextContent("16.4k");          // thought
    expect(rows[0]).toHaveTextContent("0");              // written
    expect(rows[0]).toHaveTextContent(/stopped at the token limit before any page/i);
    expect(rows[2]).toHaveTextContent("Run 3");
    // the phone list says the same in sentences
    expect(within(runs).getAllByTestId("result-run")[0]).toHaveTextContent(/thought 16.4k · wrote 0/);
  });

  it("finished: no per-run block — the numbers say it; the table shares the video's right edge", async () => {
    renderView(testDoneGreenFixture);
    const numbers = await screen.findByTestId("result-numbers");
    expect(screen.queryByTestId("result-runs")).toBeNull();
    for (const el of [numbers, ...Array.from(numbers.querySelectorAll("*"))]) {
      expect(el.getAttribute("class") ?? "").not.toMatch(/(^|\s)max-w-/);
    }
  });

  it("live: no rail, the live run owns the page", async () => {
    renderView(testRunningFixture);
    await screen.findByTestId("live-run-mock");
    expect(document.querySelector('[data-region="result-side"]')).toBeNull();
    expect(screen.getByTestId("bench-result-page").getAttribute("data-columns")).toBe("main");
  });

  it("from 1536 px the list, the main column and the rail are panes that scroll on their own", async () => {
    renderView(testDoneGreenFixture);
    await screen.findByRole("button", { name: "X-Kit" });
    for (const name of ["result-list", "result-main", "result-side"]) {
      const pane = document.querySelector(`[data-region="${name}"]`)!;
      expect(pane.className).toMatch(/(^|\s)2xl:overflow-y-auto(\s|$)/);
      expect(pane.className).toMatch(/(^|\s)2xl:h-full(\s|$)/);
      // Windows draws classic bars: the room stays reserved, nothing shifts.
      expect(pane.className).toMatch(/(^|\s)2xl:\[scrollbar-gutter:stable\](\s|$)/);
    }
  });
});

describe("ResultPage — Eingang", () => {
  it("opening a finished result resolves its Eingang item, once", async () => {
    vi.mocked(benchW1Api.tests.seen).mockClear();
    renderView(testDoneGreenFixture);
    await screen.findByTestId("bench-result-page");
    await waitFor(() => expect(benchW1Api.tests.seen).toHaveBeenCalledWith(testDoneGreenFixture.id));
    expect(benchW1Api.tests.seen).toHaveBeenCalledTimes(1);
  });

  it("a running test is not marked seen", async () => {
    vi.mocked(benchW1Api.tests.seen).mockClear();
    renderView(testRunningFixture);
    await screen.findByTestId("live-run-mock");
    expect(benchW1Api.tests.seen).not.toHaveBeenCalled();
  });
});

describe("ResultPage — two classes", () => {
  const twoClasses: BenchTestDetail = {
    ...testRunningFixture,
    state: "done",
    section: "done",
    gate: { status: "amber", reasons: [{ rule: "amber", severity: "amber", code: "single_run", params: {}, fix: null }], checksum: null },
    verdict: null,
    allowed: ["kit", "rerun"],
  };

  it("keeps class A and class B in separate blocks with a divider, no winner", async () => {
    renderView(twoClasses);
    const list = await screen.findByTestId("numbers-list");
    expect(list).toHaveTextContent("1 · Model only");
    expect(list).toHaveTextContent("2 · Agent builds (afterwards)");
    expect(screen.getByTestId("result-verdict")).toHaveTextContent(
      "Two separate results, run one after the other",
    );
    const blockB = within(list).getByTestId("block-B");
    expect(within(blockB).queryByText("Speed")).toBeNull();
  });
});

describe("ResultPage — the page needed the internet (R6)", () => {
  // Live 25.10.2026: three.js from a CDN, blocked by the recorder — the
  // video shows only the background. Red, said in words, no "page loads".
  const needsNetwork: BenchTestDetail = {
    ...testDoneGreenFixture,
    verdict: testDoneGreenFixture.verdict ? { ...testDoneGreenFixture.verdict, page_check: "unknown" } : null,
    gate: {
      status: "red",
      reasons: [{ rule: "R6", severity: "red", code: "needs_network", params: {}, fix: null }],
      checksum: testDoneGreenFixture.gate.checksum,
    },
  };

  it("says in plain words why the gate is red", async () => {
    renderView(needsNetwork);
    const gate = await screen.findByTestId("result-gate");
    expect(gate).toHaveTextContent("Not ready to post");
    expect(gate).toHaveTextContent("The page tried to load a library from the internet (blocked) — the scene stayed empty");
    expect(screen.getByTestId("result-verdict")).not.toHaveTextContent("page loads");
  });

  it("lists the offline rule among the checks: failed here, passed on a green test", async () => {
    const { unmount } = renderView(needsNetwork);
    const checks = await screen.findByRole("region", { name: "Check" });
    expect(checks).toHaveTextContent("The page tried to load a library from the internet");
    unmount();
    renderView(testDoneGreenFixture);
    expect(await screen.findByRole("region", { name: "Check" })).toHaveTextContent(
      "Page works offline: no library from the internet",
    );
  });
});

describe("ResultPage — lane lib:three-local (MC served three.js)", () => {
  // 25.10.2026: the recorder answers the page's CDN request for three.js with
  // MC's pinned copy. The page says so — chip + receipt row — and a test that
  // mixes lanes is marked instead of compared silently.
  const green = testDoneGreenFixture;
  const block = green.blocks[0];
  const first = block.contestants[0];
  const threeLocal: BenchTestDetail = {
    ...green,
    blocks: [
      {
        ...block,
        contestants: [
          {
            ...first,
            lib_track: "three-local",
            receipt: first.receipt && {
              ...first.receipt,
              lib_track: "three-local",
              libraries_provided: ["three.js r170"],
              three_requested: ["r160"],
              three_version_mismatch: true,
              three_import_map_injected: false,
            },
          },
          ...block.contestants.slice(1),
        ],
      },
      ...green.blocks.slice(1),
    ],
    gate: {
      status: "amber",
      reasons: [
        { rule: "amber", severity: "amber", code: "three_version_differs", params: {}, fix: null },
        { rule: "amber", severity: "amber", code: "lib_tracks_mixed", params: { tracks: ["none", "three-local"] }, fix: null },
      ],
      checksum: green.gate.checksum,
    },
  };

  it("shows a lane chip only on the contestant that got MC's three.js", async () => {
    renderView(threeLocal);
    const list = await screen.findByTestId("numbers-list");
    expect(within(list).getAllByText("three.js r170 from MC")).toHaveLength(1);
  });

  it("says what MC served, what the page asked for, and that it needs three.js elsewhere", async () => {
    const user = userEvent.setup();
    renderView(threeLocal);
    const buttons = await screen.findAllByRole("button", { name: /Receipt/ });
    await user.click(buttons[0]);
    const receipt = screen.getAllByTestId("receipt-A")[0];
    expect(receipt).toHaveTextContent("Library");
    expect(receipt).toHaveTextContent("three.js r170 · served locally by MC");
    expect(receipt).toHaveTextContent("page asked for r160");
    expect(receipt).toHaveTextContent("Outside the recording the page loads three.js from the internet");
  });

  it("marks the version mismatch and the mixed lanes in the gate", async () => {
    renderView(threeLocal);
    const gate = await screen.findByTestId("result-gate");
    expect(gate).toHaveTextContent("page asked for a different three.js version than the one MC serves");
    expect(gate).toHaveTextContent("Separate lanes: only some pages got three.js from MC");
  });

  it("shows no lane on a test without MC's three.js", async () => {
    renderView(green);
    await screen.findByTestId("numbers-list");
    expect(screen.queryByText(/from MC$/)).toBeNull();
  });
});

describe("ResultPage — missing", () => {
  it("shows a not-found state for an unknown id", async () => {
    get.mockRejectedValue(new Error('API 404: {"detail":"not found"}'));
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <ResultView testId="nope" />
      </QueryClientProvider>,
    );
    expect(await screen.findByTestId("not-found-state")).toHaveTextContent(
      "This test does not exist (any more).",
    );
  });
});
