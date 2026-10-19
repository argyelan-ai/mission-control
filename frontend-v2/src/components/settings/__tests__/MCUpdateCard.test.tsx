/**
 * The update card (update plan PR 5) against a mocked API: every state the
 * card can be in, the password sheet that starts an update, and the two
 * sabotage probes of the plan —
 *   (a) the status API offline for a minute → the page waits, it does NOT
 *       say "failed";
 *   (b) not an admin / switch off → no card.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { UpdateRun, UpdateStatus, UpdatePreflight } from "@/lib/mcUpdate";

const m = vi.hoisted(() => ({
  status: vi.fn(),
  preflight: vi.fn(),
  history: vi.fn(),
  start: vi.fn(),
  rollback: vi.fn(),
  resume: vi.fn(),
  stepUp: vi.fn(),
  stepUpAvailability: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  api: {
    system: {
      update: {
        status: m.status,
        preflight: m.preflight,
        history: m.history,
        start: m.start,
        rollback: m.rollback,
        resume: m.resume,
      },
    },
    auth: { stepUp: m.stepUp, stepUpAvailability: m.stepUpAvailability },
  },
}));
vi.mock("@/lib/notify", () => ({
  notify: { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() },
}));

import { MCUpdateCard, type VersionInfo } from "../MCUpdateCard";

const MIN = 60_000;
const ago = (ms: number) => new Date(Date.now() - ms).toISOString();
const FAST = { activeMs: 20, idleMs: 20, noAnswerMs: 20, reloadMs: 30, expectMs: 60_000 };

const VERSION: VersionInfo = {
  current: "0.9.2",
  latest: "v0.9.3",
  release_url: "https://example.invalid/releases/v0.9.3",
  update_available: true,
};

function run(over: Partial<UpdateRun> = {}): UpdateRun {
  return {
    run_id: "20261019T115000Z-abc123",
    phase: "pull",
    phase_state: "intent",
    from: { tag: "v0.9.2" },
    target: { tag: "v0.9.3" },
    running: true,
    interrupted: false,
    events: [],
    created_at: ago(10 * MIN),
    updated_at: ago(MIN),
    finished_at: null,
    ...over,
  };
}

function status(r: UpdateRun | null, over: Partial<UpdateStatus> = {}): UpdateStatus {
  return {
    drain: { active: false, run_id: null, expires_at: null },
    running_work: { tasks: 0, group_rounds: 0 },
    bridge_reachable: true,
    stale: false,
    run: r,
    ...over,
  };
}

const PREFLIGHT_OK: UpdatePreflight = {
  current: "v0.9.2",
  target: "v0.9.3",
  up_to_date: false,
  bridge: { reachable: true, api_version: 1 },
  checks: [
    { id: "bridge", status: "ok", message: "update door answers" },
    { id: "disk", status: "ok", message: "40000 MB free" },
    { id: "work", status: "warn", message: "1 task(s) in progress — the update waits" },
  ],
  ok: true,
  running_work: { tasks: 1, group_rounds: 0 },
  drain: { active: false, run_id: null, expires_at: null },
};

const apiErr = (code: number, detail: object | string) => new Error(`API ${code}: ${JSON.stringify({ detail })}`);

function renderCard(props: Partial<React.ComponentProps<typeof MCUpdateCard>> = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MCUpdateCard version={VERSION} timing={FAST} reload={vi.fn()} {...props} />
    </QueryClientProvider>,
  );
}

const card = () => screen.getByTestId("update-card");
const sentence = () => screen.getByTestId("update-sentence");

beforeEach(() => {
  for (const fn of Object.values(m)) fn.mockReset();
  m.history.mockResolvedValue({ runs: [] });
  m.stepUpAvailability.mockResolvedValue({ available: true, reason: null, ttl_seconds: 300 });
  try {
    sessionStorage.clear();
  } catch {
    /* ignore */
  }
});

describe("visibility", () => {
  it("switch off (404 update_disabled) → no card at all", async () => {
    m.status.mockRejectedValue(apiErr(404, { code: "update_disabled", message: "off" }));
    const { container } = renderCard();
    await waitFor(() => expect(m.status).toHaveBeenCalled());
    await new Promise((r) => setTimeout(r, 30));
    expect(container.querySelector("[data-testid=update-card]")).toBeNull();
  });

  it("not an admin (403) → no card", async () => {
    m.status.mockRejectedValue(apiErr(403, "Forbidden"));
    const { container } = renderCard();
    await waitFor(() => expect(m.status).toHaveBeenCalled());
    await new Promise((r) => setTimeout(r, 30));
    expect(container.querySelector("[data-testid=update-card]")).toBeNull();
  });

  it("managed install with the switch on → the note, no button", async () => {
    m.status.mockRejectedValue(apiErr(409, { code: "update_managed", managed: true, enabled: true }));
    renderCard();
    await waitFor(() => expect(card().dataset.state).toBe("managed"));
    expect(sentence()).toHaveTextContent("updated through its deploy procedure");
    expect(within(card()).queryByRole("button")).toBeNull();
  });

  it("managed install with the switch still off → nothing new on screen", async () => {
    m.status.mockRejectedValue(apiErr(409, { code: "update_managed", managed: true, enabled: false }));
    const { container } = renderCard();
    await waitFor(() => expect(m.status).toHaveBeenCalled());
    await new Promise((r) => setTimeout(r, 30));
    expect(container.querySelector("[data-testid=update-card]")).toBeNull();
  });
});

describe("available → check → lights → password → start", () => {
  it("walks the whole way and sends the step-up token with the start", async () => {
    let current = status(null);
    m.status.mockImplementation(async () => current);
    m.preflight.mockResolvedValue(PREFLIGHT_OK);
    m.stepUp.mockResolvedValue({ step_up_token: "tok-1", expires_in: 300 });
    m.start.mockImplementation(async () => {
      current = status(run({ run_id: "20261019T120000Z-aaaaaa", phase: "drain" }), { running_work: { tasks: 2, group_rounds: 0 } });
      return { run_id: "20261019T120000Z-aaaaaa", state: "started", target: "v0.9.3" };
    });
    renderCard();

    await waitFor(() => expect(card().dataset.state).toBe("available"));
    expect(sentence()).toHaveTextContent("Version 0.9.3 is available · you have 0.9.2");

    await userEvent.click(screen.getByTestId("update-check"));
    await waitFor(() => expect(card().dataset.state).toBe("ready"));
    expect(m.preflight).toHaveBeenCalledWith("v0.9.3");
    const lights = screen.getAllByTestId("update-check").filter((el) => el.tagName === "LI");
    // red/yellow first
    expect(lights[0].dataset.status).toBe("warn");
    expect(lights.map((l) => l.dataset.check)).toEqual(["work", "bridge", "disk"]);

    await userEvent.click(screen.getByTestId("update-now"));
    const sheet = await screen.findByTestId("update-sheet");
    expect(sheet.dataset.mode).toBe("start");
    expect(within(sheet).getByText(/unreachable for about 2–10 minutes/)).toBeInTheDocument();
    expect(screen.getByTestId("update-sheet-confirm")).toBeDisabled();
    await userEvent.type(screen.getByTestId("update-sheet-password"), "s3cret");
    await userEvent.click(screen.getByTestId("update-sheet-confirm"));

    await waitFor(() => expect(m.start).toHaveBeenCalledWith("v0.9.3", "tok-1"));
    expect(m.stepUp).toHaveBeenCalledWith("s3cret");
    await waitFor(() => expect(card().dataset.state).toBe("waiting"));
    expect(sentence()).toHaveTextContent("Waiting for quiet: 2 tasks are still running.");
  });

  it("a red check locks the button and says why", async () => {
    m.status.mockResolvedValue(status(null));
    m.preflight.mockResolvedValue({
      ...PREFLIGHT_OK,
      ok: false,
      checks: [
        { id: "disk", status: "fail", message: "900 MB free, 3000 MB needed" },
        { id: "bridge", status: "ok", message: "ok" },
      ],
    });
    renderCard();
    await waitFor(() => expect(card().dataset.state).toBe("available"));
    await userEvent.click(screen.getByTestId("update-check"));
    await waitFor(() => expect(card().dataset.state).toBe("blocked"));
    expect(screen.getByTestId("update-verdict")).toHaveTextContent("One check is red");
    expect(screen.queryByTestId("update-now")).toBeNull();
    expect(screen.getByText("Not enough disk space for the new images and the backup")).toBeInTheDocument();
  });

  it("a wrong password stays in the sheet with the reason", async () => {
    m.status.mockResolvedValue(status(null));
    m.preflight.mockResolvedValue(PREFLIGHT_OK);
    m.stepUp.mockRejectedValue(apiErr(401, { code: "step_up_wrong_password", message: "Wrong password." }));
    renderCard();
    await waitFor(() => expect(card().dataset.state).toBe("available"));
    await userEvent.click(screen.getByTestId("update-check"));
    await userEvent.click(await screen.findByTestId("update-now"));
    await userEvent.type(screen.getByTestId("update-sheet-password"), "nope");
    await userEvent.click(screen.getByTestId("update-sheet-confirm"));
    expect(await screen.findByTestId("update-sheet-error")).toHaveTextContent("Wrong password.");
    expect(m.start).not.toHaveBeenCalled();
  });

  it("bridge not answering at start: the token comes back, a retry needs no second password", async () => {
    m.status.mockResolvedValue(status(null));
    m.preflight.mockResolvedValue(PREFLIGHT_OK);
    m.stepUp.mockResolvedValue({ step_up_token: "tok-2", expires_in: 300 });
    m.start.mockRejectedValueOnce(apiErr(503, { code: "bridge_unreachable", command: "./install.sh --bridge" }));
    m.start.mockResolvedValueOnce({ run_id: "20261019T120000Z-bbbbbb", state: "started" });
    renderCard();
    await waitFor(() => expect(card().dataset.state).toBe("available"));
    await userEvent.click(screen.getByTestId("update-check"));
    await userEvent.click(await screen.findByTestId("update-now"));
    await userEvent.type(screen.getByTestId("update-sheet-password"), "s3cret");
    await userEvent.click(screen.getByTestId("update-sheet-confirm"));
    const err = await screen.findByTestId("update-sheet-error");
    expect(err.dataset.error).toBe("bridgeUnreachable");
    expect(within(err).getByText("./install.sh --bridge")).toBeInTheDocument();
    expect(screen.queryByTestId("update-sheet-password")).toBeNull();
    await userEvent.click(screen.getByTestId("update-sheet-confirm"));
    await waitFor(() => expect(m.start).toHaveBeenLastCalledWith("v0.9.3", "tok-2"));
    expect(m.stepUp).toHaveBeenCalledTimes(1);
  });

  it("an account without a password gets the terminal command instead of the button", async () => {
    m.status.mockResolvedValue(status(null));
    m.preflight.mockResolvedValue(PREFLIGHT_OK);
    m.stepUpAvailability.mockResolvedValue({ available: false, reason: "step_up_no_password", ttl_seconds: 300 });
    renderCard();
    await waitFor(() => expect(card().dataset.state).toBe("available"));
    await userEvent.click(screen.getByTestId("update-check"));
    await waitFor(() => expect(card().dataset.state).toBe("ready"));
    expect(screen.queryByTestId("update-now")).toBeNull();
    const note = screen.getByTestId("update-terminal-only");
    expect(note).toHaveTextContent("has no password");
    expect(within(note).getByText("./install.sh --update")).toBeInTheDocument();
  });
});

describe("the states of a run", () => {
  const cases: [string, UpdateStatus, string, RegExp][] = [
    ["up to date", status(null), "upToDate", /MC 0\.9\.2 is up to date/],
    ["waiting for quiet", status(run({ phase: "drain" }), { running_work: { tasks: 1, group_rounds: 2 } }), "waiting", /1 task is still running · 2 group-chat rounds are still running/],
    ["running", status(run({ phase: "backup" })), "running", /Updating to 0\.9\.3/],
    ["success", status(run({ phase: "done", running: false, finished_at: ago(MIN), events: [{ phase: "backup", event: "done", at: ago(5 * MIN) }] })), "success", /MC 0\.9\.3 is running/],
    ["set back", status(run({ phase: "rolled_back", running: false, finished_at: ago(MIN), events: [{ phase: "verify", event: "intent" }, { phase: "rolling_back", event: "intent" }] })), "rolledBack", /failed at “Check”\. MC was set back to 0\.9\.2 automatically/],
    ["interrupted, bridge there", status(run({ phase: "up", running: false, interrupted: true })), "interrupted", /interrupted and is being resumed/],
    ["interrupted, bridge gone", status(run({ phase: "up", running: false }), { stale: true, bridge_reachable: false, command: "./install.sh --resume" }), "interrupted", /On the machine, in the install folder/],
    ["way back failed", status(run({ phase: "rollback_failed", running: false, finished_at: ago(MIN) }), { command: "./install.sh --rollback" }), "rollbackFailed", /setting MC back failed too/],
    ["download failed", status(run({ phase: "aborted", running: false, failure_kind: "network", finished_at: ago(MIN) })), "aborted", /The download failed\. MC still runs 0\.9\.2, unchanged\./],
  ];

  it.each(cases)("%s", async (_name, st, state, text) => {
    m.status.mockResolvedValue(st);
    renderCard({ version: state === "upToDate" ? { ...VERSION, latest: "v0.9.2", update_available: false } : VERSION });
    await waitFor(() => expect(card().dataset.state).toBe(state));
    expect(sentence().textContent).toMatch(text);
  });

  it("success shows the backup kept for 14 days and offers the way back", async () => {
    m.status.mockResolvedValue(
      status(run({ phase: "done", running: false, finished_at: ago(MIN), events: [{ phase: "backup", event: "done", at: ago(5 * MIN) }] })),
    );
    renderCard();
    await waitFor(() => expect(card().dataset.state).toBe("success"));
    expect(screen.getByTestId("update-backup")).toHaveTextContent("kept for 14 days");
    expect(await screen.findByTestId("update-go-back")).toHaveTextContent("Back to 0.9.2");
  });

  it("the way back asks for the password and warns what is lost", async () => {
    m.status.mockResolvedValue(
      status(run({ phase: "done", running: false, finished_at: ago(MIN), events: [{ phase: "backup", event: "done", at: ago(5 * MIN) }] })),
    );
    m.stepUp.mockResolvedValue({ step_up_token: "tok-3", expires_in: 300 });
    m.rollback.mockResolvedValue({ run_id: "20261019T115000Z-abc123", state: "started" });
    renderCard();
    await userEvent.click(await screen.findByTestId("update-go-back"));
    const sheet = await screen.findByTestId("update-sheet");
    expect(sheet.dataset.mode).toBe("rollback");
    expect(sheet).toHaveTextContent("Everything written since then is lost");
    await userEvent.type(screen.getByTestId("update-sheet-password"), "s3cret");
    await userEvent.click(screen.getByTestId("update-sheet-confirm"));
    await waitFor(() => expect(m.rollback).toHaveBeenCalledWith("20261019T115000Z-abc123", "tok-3"));
    await waitFor(() => expect(card().dataset.state).toBe("pending"));
    expect(sentence()).toHaveTextContent("Setting MC back to 0.9.2");
  });

  it("set back: copy log puts the journal lines on the clipboard", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    m.status.mockResolvedValue(
      status(run({ phase: "rolled_back", running: false, finished_at: ago(MIN), error: "backend not healthy", events: [{ phase: "verify", event: "intent", detail: "four checks" }, { phase: "rolling_back", event: "intent" }] })),
    );
    renderCard();
    await userEvent.click(await screen.findByTestId("update-copy-log"));
    expect(writeText).toHaveBeenCalledWith(expect.stringContaining("error: backend not healthy"));
  });

  it("the phase bar shows step 4 of 5 while MC restarts", async () => {
    m.status.mockResolvedValue(status(run({ phase: "up" })));
    renderCard();
    await waitFor(() => expect(card().dataset.state).toBe("running"));
    expect(screen.getByTestId("update-step-label")).toHaveTextContent("Step 4 of 5 · Restart");
  });
});

describe("the restart gap", () => {
  // Sabotage probe (a) of the plan: the status API is gone for a minute.
  it("status API offline (≈ 60 s of polls) → the page waits; it never says 'failed'; then success", async () => {
    let calls = 0;
    let offline = false;
    const reload = vi.fn();
    m.status.mockImplementation(async () => {
      calls += 1;
      if (offline) throw new TypeError("Failed to fetch");
      return status(run({ phase: calls < 3 ? "switch" : "up" }));
    });
    renderCard({ reload });
    await waitFor(() => expect(card().dataset.state).toBe("running"));
    offline = true;
    await waitFor(() => expect(card().dataset.state).toBe("restarting"));
    expect(sentence()).toHaveTextContent("MC is restarting, this page reconnects in a moment.");

    // 12 failed polls = a minute at the real 5 s interval.
    const at = calls;
    await waitFor(() => expect(calls).toBeGreaterThanOrEqual(at + 12), { timeout: 3000 });
    expect(card().dataset.state).toBe("restarting");
    expect(card().textContent).not.toMatch(/fail|failed|error/i);

    m.status.mockImplementation(async () =>
      status(run({ phase: "done", running: false, finished_at: new Date().toISOString(), events: [{ phase: "backup", event: "done", at: ago(2 * MIN) }] })),
    );
    offline = false;
    await waitFor(() => expect(card().dataset.state).toBe("success"));
    // Watched live → the page reloads once to load the new version's pages.
    await waitFor(() => expect(reload).toHaveBeenCalledTimes(1));
  });

  it("a proxy 502 page is a restart too, not an error", async () => {
    let offline = false;
    m.status.mockImplementation(async () => {
      if (offline) throw new Error("API 502: <html><body>Bad Gateway</body></html>");
      return status(run({ phase: "up" }));
    });
    renderCard();
    await waitFor(() => expect(card().dataset.state).toBe("running"));
    offline = true;
    await waitFor(() => expect(card().dataset.state).toBe("restarting"));
  });

  it("no run under way and no answer → 'cannot be read', not 'restarting'", async () => {
    m.status.mockRejectedValue(new TypeError("Failed to fetch"));
    renderCard();
    await waitFor(() => expect(card().dataset.state).toBe("unknown"));
  });

  it("cancel while waiting for quiet asks for the password and calls the way back", async () => {
    m.status.mockResolvedValue(status(run({ phase: "drain" }), { running_work: { tasks: 1, group_rounds: 0 } }));
    m.stepUp.mockResolvedValue({ step_up_token: "tok-4", expires_in: 300 });
    m.rollback.mockResolvedValue({ cancel_requested: true, run_id: "20261019T115000Z-abc123" });
    renderCard();
    await userEvent.click(await screen.findByTestId("update-cancel"));
    expect((await screen.findByTestId("update-sheet")).dataset.mode).toBe("cancel");
    await userEvent.type(screen.getByTestId("update-sheet-password"), "s3cret");
    await act(async () => {
      await userEvent.click(screen.getByTestId("update-sheet-confirm"));
    });
    await waitFor(() => expect(m.rollback).toHaveBeenCalledWith("20261019T115000Z-abc123", "tok-4"));
  });
});
