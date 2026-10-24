import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { BenchTestDetail } from "../../types";
import {
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
      tests: { get: vi.fn(), rerun: vi.fn(), harvest: vi.fn(), stop: vi.fn() },
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
    expect(warning).toHaveTextContent("Run 2 stopped at 16,384 tokens");
    expect(warning).toHaveTextContent("Only you see this");
    const kit = screen.getByRole("button", { name: "X-Kit" });
    // DOM order: warning first, then the button.
    expect(warning.compareDocumentPosition(kit) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getAllByText(/runs hit the token limit/)).toHaveLength(1);
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
    const ask = await screen.findByRole("button", { name: "Start · $0.20 — OK?" });
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
