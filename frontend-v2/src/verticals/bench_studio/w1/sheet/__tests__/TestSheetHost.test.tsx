// Package B — the W1 test sheet (P3). Contract: TestSheetHost has no props,
// reads `/bench?test=1&…` (lib/benchLinks), loads GET /bench/plan, starts
// with POST /bench/tests and lands on /bench/r/<id>. These tests pin the
// operator-facing promises: 2 taps / 0 typed for the live model, money per
// D3/D4 incl. the two-step confirmation, blockers before Start, the effort
// ladder as an explicit multi-select, the opponent note (N7), class mix.
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { benchTestHref } from "@/lib/benchLinks";
import type { BenchPlan, Contestant } from "../../types";
import {
  contestantsFixture,
  planStandardFixture,
  testRunningFixture,
} from "../../fixtures";

const nav = vi.hoisted(() => ({ search: "", replace: vi.fn(), push: vi.fn() }));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: nav.replace, push: nav.push }),
  usePathname: () => "/bench",
  useSearchParams: () => new URLSearchParams(nav.search),
}));

const api = vi.hoisted(() => ({
  plan: vi.fn(),
  contestants: vi.fn(),
  create: vi.fn(),
}));

vi.mock("../../api", async (importOriginal) => {
  const orig = await importOriginal<typeof import("../../api")>();
  return {
    ...orig,
    benchW1Api: {
      ...orig.benchW1Api,
      plan: api.plan,
      contestants: api.contestants,
      tests: { ...orig.benchW1Api.tests, create: api.create },
    },
  };
});

import { TestSheetHost } from "../TestSheetHost";

function clone<T>(v: T): T {
  return JSON.parse(JSON.stringify(v)) as T;
}

function renderHost(search: string) {
  nav.search = search;
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <TestSheetHost />
    </QueryClientProvider>,
  );
}

const fromRuntimes = benchTestHref({
  runtimeId: "11111111-1111-4111-8111-111111111111",
  trigger: "runtimes",
}).split("?")[1];

async function sheet() {
  return screen.findByRole("dialog", { name: "Test a model" });
}

async function startButton() {
  const d = await sheet();
  return within(d).findByTestId("bench-sheet-start");
}

beforeEach(() => {
  nav.replace.mockReset();
  nav.push.mockReset();
  api.plan.mockReset().mockResolvedValue(clone(planStandardFixture));
  api.contestants.mockReset().mockResolvedValue(clone(contestantsFixture));
  api.create.mockReset().mockResolvedValue(clone(testRunningFixture));
});

describe("TestSheetHost — opens from the URL", () => {
  it("renders nothing and loads nothing without test=1", () => {
    const { container } = renderHost("challenge=abc");
    expect(container.innerHTML).toBe("");
    expect(api.plan).not.toHaveBeenCalled();
  });

  it("loads the plan for the runtime from the link and shows every row pre-filled", async () => {
    renderHost(fromRuntimes);
    const d = await sheet();
    await within(d).findByText("DeepSeek-V4.1-Flash-1017");
    expect(api.plan).toHaveBeenCalledWith(
      expect.objectContaining({ runtimeId: "11111111-1111-4111-8111-111111111111" }),
    );
    expect(within(d).getByText("DGX Spark ×2 · local")).toBeInTheDocument();
    expect(within(d).getByText("Standard · Pagoda Garden")).toBeInTheDocument();
    expect(within(d).getByText("3 runs · same prompt")).toBeInTheDocument();
    // Harness chips from the runtime binding, effort chips from the real probe.
    expect(within(d).getByRole("button", { name: "Raw API" })).toHaveAttribute("aria-pressed", "true");
    expect(within(d).getByRole("button", { name: "omp (local agent)" })).toHaveAttribute("aria-pressed", "false");
    expect(within(d).getByRole("button", { name: "high (default)" })).toHaveAttribute("aria-pressed", "true");
    expect(within(d).getByRole("button", { name: "off" })).toBeInTheDocument();
    expect(within(d).getByRole("button", { name: "max" })).toBeInTheDocument();
    expect(within(d).getByText(/levels checked on/)).toBeInTheDocument();
    expect(within(d).getByText("DGX Spark ×2 · eco · free")).toBeInTheDocument();
    // D3: local + the small hypothetical API price.
    expect(within(d).getByText(/local · costs nothing/)).toBeInTheDocument();
    expect(within(d).getByText(/API price would be ≈ \$0\.04/)).toBeInTheDocument();
    expect(await startButton()).toHaveTextContent(/^Start$/);
  });

  it("tests the live model in 2 taps and 0 typed fields: Testen (link) → Start", async () => {
    const user = userEvent.setup();
    const typeSpy = vi.spyOn(user, "type");
    // Tap 1 happened on the Runtimes card: it produced exactly this link.
    renderHost(fromRuntimes);
    const start = await startButton();
    await waitFor(() => expect(start).toBeEnabled());
    // Tap 2:
    await user.click(start);
    await waitFor(() => expect(api.create).toHaveBeenCalledTimes(1));
    expect(api.create).toHaveBeenCalledWith(
      expect.objectContaining({
        preset: "standard",
        prompt_template_id: planStandardFixture.preset.prompt.template_id,
        picks: planStandardFixture.picks,
        trigger: "runtimes",
      }),
    );
    await waitFor(() => expect(nav.push).toHaveBeenCalledWith(`/bench/r/${testRunningFixture.id}`));
    expect(typeSpy).not.toHaveBeenCalled();
    expect(screen.queryAllByRole("textbox")).toHaveLength(0);
  });

  it("closing removes the sheet params and keeps the rest of the URL", async () => {
    const user = userEvent.setup();
    renderHost(`tab=library&${fromRuntimes}`);
    const d = await sheet();
    await user.click(within(d).getByRole("button", { name: "Close" }));
    expect(nav.replace).toHaveBeenCalledWith("/bench?tab=library", { scroll: false });
  });

  it("shows a retry when the plan does not load", async () => {
    api.plan.mockRejectedValueOnce(new Error("API 500: boom"));
    const user = userEvent.setup();
    renderHost(fromRuntimes);
    const d = await sheet();
    await within(d).findByText("The plan could not be loaded.");
    await user.click(within(d).getByRole("button", { name: "Try again" }));
    await within(d).findByText("DeepSeek-V4.1-Flash-1017");
  });
});

describe("TestSheetHost — effort", () => {
  it("one tap on an effort chip sends that level", async () => {
    const user = userEvent.setup();
    renderHost(fromRuntimes);
    const d = await sheet();
    await user.click(await within(d).findByRole("button", { name: "max" }));
    await user.click(await startButton());
    await waitFor(() => expect(api.create).toHaveBeenCalled());
    expect(api.create.mock.calls[0][0].picks).toEqual([
      { runtime_id: "11111111-1111-4111-8111-111111111111", harness: "api", efforts: ["max"] },
    ]);
  });

  it("the effort ladder is an explicit multi-select with checks and a count", async () => {
    const plan = clone(planStandardFixture);
    plan.preset = clone(plan.presets[2]);
    plan.picks[0].efforts = ["off", "default", "max"];
    api.plan.mockResolvedValue(plan);
    const user = userEvent.setup();
    renderHost(`${fromRuntimes}&preset=effort_ladder`);
    const d = await sheet();
    await within(d).findByText("Effort ladder · Pagoda Garden");
    expect(within(d).getByText("3 levels × 3 runs")).toBeInTheDocument();
    expect(within(d).getByText(/3 selected · several possible/)).toBeInTheDocument();
    for (const name of ["off", "high (default)", "max"]) {
      expect(within(d).getByRole("button", { name })).toHaveAttribute("aria-pressed", "true");
    }
    expect(within(d).getByText(/max can think for a long time/)).toBeInTheDocument();
    await user.click(within(d).getByRole("button", { name: "max" }));
    expect(within(d).getByText(/2 selected · several possible/)).toBeInTheDocument();
    await user.click(within(d).getByRole("button", { name: "off" }));
    expect(within(d).getByText("Pick at least 2 levels for the ladder")).toBeInTheDocument();
    expect(await startButton()).toBeDisabled();
  });

  it.each([
    ["only_on_off", "This model only knows on/off"],
    ["no_effect", "Default only: no switch changes the thinking"],
    ["failed", "Check failed"],
  ] as const)("prints the probe outcome %s", async (status, text) => {
    const plan = clone(planStandardFixture);
    plan.contestants[0].effort.status = status;
    if (status !== "only_on_off") plan.contestants[0].effort.options = [plan.contestants[0].effort.options[0]];
    api.plan.mockResolvedValue(plan);
    renderHost(fromRuntimes);
    const d = await sheet();
    expect(await within(d).findByText(new RegExp(text))).toBeInTheDocument();
    if (status === "failed") {
      expect(within(d).getByText(/The result then shows effort as unknown/)).toBeInTheDocument();
    }
  });
});

describe("TestSheetHost — harness and class mix", () => {
  it("API + omp runs one after the other, two picks, effort display-only for omp", async () => {
    const user = userEvent.setup();
    renderHost(fromRuntimes);
    const d = await sheet();
    await user.click(await within(d).findByRole("button", { name: "omp (local agent)" }));
    expect(within(d).getByText(/One after the other/)).toBeInTheDocument();
    expect(within(d).getByText(/omp session: high/)).toBeInTheDocument();
    // Max 2 contestants: no room for an opponent next to two harnesses.
    expect(within(d).queryByRole("button", { name: "Add opponent" })).not.toBeInTheDocument();
    await user.click(await startButton());
    await waitFor(() => expect(api.create).toHaveBeenCalled());
    expect(api.create.mock.calls[0][0].picks).toEqual([
      { runtime_id: "11111111-1111-4111-8111-111111111111", harness: "api", efforts: ["default"] },
      { runtime_id: "11111111-1111-4111-8111-111111111111", harness: "omp", efforts: ["default"] },
    ]);
  });

  it("omp alone shows the session effort, not chips", async () => {
    const user = userEvent.setup();
    renderHost(fromRuntimes);
    const d = await sheet();
    await user.click(await within(d).findByRole("button", { name: "omp (local agent)" }));
    await user.click(within(d).getByRole("button", { name: "Raw API" }));
    expect(within(d).getByText("omp session: high")).toBeInTheDocument();
    expect(within(d).getByText("Shown only, not settable yet")).toBeInTheDocument();
    expect(within(d).queryByRole("button", { name: "max" })).not.toBeInTheDocument();
    expect(within(d).getByText("1 run · agent builds")).toBeInTheDocument();
  });
});

describe("TestSheetHost — opponent and money (D3/D4, N7)", () => {
  async function addOpponent(user: ReturnType<typeof userEvent.setup>, name: string) {
    const d = await sheet();
    await user.click(await within(d).findByRole("button", { name: "Add opponent" }));
    await user.click(await within(d).findByRole("button", { name: new RegExp(`^${name}`) }));
    return d;
  }

  it("a cloud opponent is labelled N7 and starts via the subscription, no $ on the button", async () => {
    const user = userEvent.setup();
    renderHost(fromRuntimes);
    const d = await addOpponent(user, "deepseek-v4.1-flash");
    expect(within(d).getByText(/same model name · cloud version unknown/)).toBeInTheDocument();
    const start = await startButton();
    expect(start).toHaveTextContent("Start · via your subscription");
    expect(start.textContent).not.toContain("$");
    expect(within(d).getByText(/Runs on your Ollama Cloud subscription: no extra cost/)).toBeInTheDocument();
    expect(within(d).getByText(/2 contestants · 3 runs each/)).toBeInTheDocument();
    await user.click(start);
    await waitFor(() => expect(api.create).toHaveBeenCalled());
    expect(api.create.mock.calls[0][0].picks).toHaveLength(2);
    expect(api.create.mock.calls[0][0].picks[1]).toEqual({
      runtime_id: "22222222-2222-4222-8222-222222222222",
      harness: "api",
      efforts: ["default"],
    });
    expect(api.create.mock.calls[0][0].cost_confirmed_usd ?? null).toBeNull();
  });

  it("a paid run over $1 needs two taps of OK before anything starts", async () => {
    const user = userEvent.setup();
    renderHost(fromRuntimes);
    await addOpponent(user, "example-frontier-1");
    const start = await startButton();
    expect(start).toHaveTextContent("Start · $1.35 — OK?");
    await user.click(start);
    expect(api.create).not.toHaveBeenCalled();
    expect(start).toHaveTextContent("Over $1: really start for $1.35?");
    await user.click(start);
    await waitFor(() => expect(api.create).toHaveBeenCalledTimes(1));
    expect(api.create.mock.calls[0][0]).toEqual(
      expect.objectContaining({ cost_confirmed_usd: 1.35, confirm_over_cap: true }),
    );
  });

  it("removing the opponent brings back the plain Start", async () => {
    const user = userEvent.setup();
    renderHost(fromRuntimes);
    const d = await addOpponent(user, "example-frontier-1");
    await user.click(within(d).getByRole("button", { name: "Remove opponent" }));
    expect(await startButton()).toHaveTextContent(/^Start$/);
  });

  it("a 409 from the server turns Start into the price question, the next tap confirms", async () => {
    api.create.mockRejectedValueOnce(
      new Error(
        'API 409: {"detail":{"code":"bench_cost_confirmation","step":1,"usd":0.2,"cap_usd":1.0}}',
      ),
    );
    const user = userEvent.setup();
    renderHost(fromRuntimes);
    const start = await startButton();
    await user.click(start);
    await waitFor(() => expect(start).toHaveTextContent("Start · $0.20 — OK?"));
    expect(nav.push).not.toHaveBeenCalled();
    await user.click(start);
    await waitFor(() => expect(api.create).toHaveBeenCalledTimes(2));
    expect(api.create.mock.calls[1][0]).toEqual(expect.objectContaining({ cost_confirmed_usd: 0.2 }));
    expect(api.create.mock.calls[1][0].confirm_over_cap ?? false).toBe(false);
  });

  it("a 409 step 2 asks the cap question and the next tap sends confirm_over_cap", async () => {
    api.create.mockRejectedValueOnce(
      new Error(
        'API 409: {"detail":{"code":"bench_cost_confirmation","step":2,"usd":2.5,"cap_usd":1.0}}',
      ),
    );
    const user = userEvent.setup();
    renderHost(fromRuntimes);
    const start = await startButton();
    await user.click(start);
    await waitFor(() => expect(start).toHaveTextContent("Over $1: really start for $2.50?"));
    await user.click(start);
    await waitFor(() => expect(api.create).toHaveBeenCalledTimes(2));
    expect(api.create.mock.calls[1][0]).toEqual(
      expect.objectContaining({ cost_confirmed_usd: 2.5, confirm_over_cap: true }),
    );
  });

  it("another server error stays in the sheet and names the problem", async () => {
    api.create.mockRejectedValueOnce(new Error("API 500: engine exploded"));
    const user = userEvent.setup();
    renderHost(fromRuntimes);
    await user.click(await startButton());
    expect(await screen.findByRole("alert")).toHaveTextContent("Start failed");
    expect(nav.push).not.toHaveBeenCalled();
  });
});

describe("TestSheetHost — server refusals in words (K10)", () => {
  it("a blocked start names the blocker, never the raw answer", async () => {
    api.create.mockRejectedValueOnce(
      new Error(
        'API 409: {"detail":{"code":"bench_blocked","message":"blocked","blockers":[{"code":"engine_down","blocking":true}]}}',
      ),
    );
    const user = userEvent.setup();
    renderHost(fromRuntimes);
    await user.click(await startButton());
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Engine not running");
    expect(alert.textContent).not.toMatch(/bench_blocked|engine_down|\{/);
    expect(nav.push).not.toHaveBeenCalled();
  });

  it("an unavailable harness says why in words", async () => {
    const plan: BenchPlan = clone(planStandardFixture);
    const omp = plan.contestants[0].harnesses.find((o) => o.harness === "omp");
    expect(omp).toBeTruthy();
    omp!.available = false;
    omp!.reason = { code: "agent_paused", params: {} };
    api.plan.mockResolvedValue(plan);
    renderHost(fromRuntimes);
    const d = await sheet();
    expect(await within(d).findByText(/the agent is paused \(quiet mode\)/)).toBeInTheDocument();
  });
});

describe("TestSheetHost — blockers before Start", () => {
  function withBlocker(blocking: boolean, code: Contestant["blockers"][number]["code"], until: string | null = null) {
    const plan: BenchPlan = clone(planStandardFixture);
    plan.blockers = [{ code, blocking, until, params: {} }];
    plan.contestants[0].blockers = [...plan.blockers];
    if (code === "box_busy") plan.contestants[0].device!.state = "busy";
    if (code === "box_asleep") plan.contestants[0].device!.state = "asleep";
    api.plan.mockResolvedValue(plan);
  }

  it("a blocking problem disables Start and says why", async () => {
    withBlocker(true, "box_asleep");
    renderHost(fromRuntimes);
    const d = await sheet();
    expect(await within(d).findByText("Box is asleep")).toBeInTheDocument();
    expect(within(d).getByText("Start works once this is fixed.")).toBeInTheDocument();
    expect(await startButton()).toBeDisabled();
  });

  it("a waiting problem keeps Start and explains when the test runs", async () => {
    withBlocker(false, "box_busy");
    renderHost(fromRuntimes);
    const d = await sheet();
    expect(await within(d).findByText("Box busy")).toBeInTheDocument();
    expect(within(d).getByText(/after 10 min at the latest/)).toBeInTheDocument();
    expect(within(d).getByText("DGX Spark ×2 · eco · busy")).toBeInTheDocument();
    expect(await startButton()).toBeEnabled();
  });
});

describe("TestSheetHost — presets", () => {
  it("switching to Quick look reloads the plan with that preset", async () => {
    const user = userEvent.setup();
    renderHost(fromRuntimes);
    const d = await sheet();
    await within(d).findByText("Standard · Pagoda Garden");
    await user.click(within(d).getByRole("button", { name: "Choose another test" }));
    await user.click(await within(d).findByRole("button", { name: /^Quick look/ }));
    await waitFor(() =>
      expect(api.plan).toHaveBeenLastCalledWith(expect.objectContaining({ preset: "quick" })),
    );
  });

  it("offers Again only for a re-run link", async () => {
    const user = userEvent.setup();
    renderHost(fromRuntimes);
    const d = await sheet();
    await user.click(await within(d).findByRole("button", { name: "Choose another test" }));
    expect(within(d).queryByRole("button", { name: /^Again/ })).not.toBeInTheDocument();
  });
});
