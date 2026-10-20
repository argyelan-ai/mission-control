/**
 * The update card's rules (lib/mcUpdate.ts, update plan PR 5): one status
 * answer → exactly one state. The two promises the plan makes are pinned
 * here: a status call that does not answer while MC restarts is "restarting"
 * (asked up to 25 minutes), never "failed"; and the phase bar never runs
 * backwards.
 */
import { describe, it, expect } from "vitest";
import {
  RESTART_PATIENCE_MS,
  STALE_GRACE_MS,
  canGoBack,
  deriveRunState,
  expectArrived,
  failedStep,
  isActive,
  isNoAnswer,
  parseUpdateError,
  statusGate,
  wasRequested,
  type DeriveInput,
  type UpdateRun,
  type UpdateStatus,
} from "../mcUpdate";

const NOW = Date.parse("2026-10-19T12:00:00Z");
const ago = (ms: number) => new Date(NOW - ms).toISOString();
const MIN = 60_000;

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
    drain: { active: !!r && !["done", "aborted", "rolled_back"].includes(r.phase), run_id: r?.run_id ?? null, expires_at: null },
    running_work: { tasks: 0, group_rounds: 0 },
    bridge_reachable: true,
    stale: false,
    run: r,
    ...over,
  };
}

function derive(over: Partial<DeriveInput>) {
  return deriveRunState({
    status: null,
    now: NOW,
    seenActive: false,
    maxStep: 0,
    noAnswerSince: null,
    activeKnown: false,
    staleSince: null,
    ...over,
  });
}

describe("parseUpdateError", () => {
  it("reads the code, the command and the managed/enabled flags", () => {
    const e = parseUpdateError(
      new Error('API 409: {"detail":{"code":"update_managed","message":"m","managed":true,"enabled":true}}'),
    );
    expect(e).toMatchObject({ status: 409, code: "update_managed", managed: true, enabled: true });
    expect(parseUpdateError(new Error('API 503: {"detail":{"code":"bridge_unreachable","command":"./install.sh --bridge"}}')).command).toBe(
      "./install.sh --bridge",
    );
  });

  it("a fetch that got no answer at all, or a proxy page, is 'no answer'", () => {
    expect(isNoAnswer(parseUpdateError(new TypeError("Failed to fetch")))).toBe(true);
    expect(isNoAnswer(parseUpdateError(new Error("API 502: <html>Bad Gateway</html>")))).toBe(true);
    expect(isNoAnswer(parseUpdateError(new Error("API 500: Internal Server Error")))).toBe(true);
    // An answer from MC itself is not a restart.
    expect(isNoAnswer(parseUpdateError(new Error('API 502: {"detail":{"code":"update_engine_error"}}')))).toBe(false);
    expect(isNoAnswer(parseUpdateError(new Error('API 404: {"detail":{"code":"update_disabled"}}')))).toBe(false);
  });
});

describe("statusGate — the card is only there when the switch is on", () => {
  const err = (status: number, detail: object) => parseUpdateError(new Error(`API ${status}: ${JSON.stringify({ detail })}`));

  it("switch off (404) → no card", () => {
    expect(statusGate(err(404, { code: "update_disabled" }))).toBe("hidden");
  });
  it("not an admin (403) → no card", () => {
    expect(statusGate(err(403, { detail: "forbidden" }))).toBe("hidden");
  });
  it("managed install: the note only once the switch is on", () => {
    expect(statusGate(err(409, { code: "update_managed", managed: true, enabled: false }))).toBe("hidden");
    expect(statusGate(err(409, { code: "update_managed", managed: true, enabled: true }))).toBe("managed");
  });
  it("a missing answer keeps the card on screen (restart gap)", () => {
    expect(statusGate(parseUpdateError(new TypeError("Failed to fetch")))).toBe("shown");
  });
});

describe("deriveRunState — the ten states", () => {
  it("no run → idle (the card then shows 'available' or 'up to date')", () => {
    expect(derive({ status: status(null) }).kind).toBe("idle");
  });

  it("drain with running work → waiting, with the counts", () => {
    const s = derive({ status: status(run({ phase: "drain" }), { running_work: { tasks: 2, group_rounds: 1 } }) });
    expect(s.kind).toBe("waiting");
    expect(s.kind === "waiting" && s.work).toEqual({ tasks: 2, group_rounds: 1 });
  });

  it("pull / backup / switch / up / verify → running on step 1..5", () => {
    const steps = ["pull", "backup", "switch", "up", "verify"].map((phase) => {
      const s = derive({ status: status(run({ phase })) });
      return s.kind === "running" ? s.step : null;
    });
    expect(steps).toEqual([0, 1, 2, 3, 4]);
  });

  it("the phase bar never runs backwards", () => {
    // e.g. a resumed run reports "switch" again after the page showed "up"
    const s = derive({ status: status(run({ phase: "switch" })), maxStep: 3 });
    expect(s.kind === "running" && s.step).toBe(3);
  });

  it("no answer during a run → restarting, and still restarting after 60 s (never 'failed')", () => {
    const last = status(run({ phase: "up" }));
    for (const gone of [5_000, 60_000, 10 * MIN, RESTART_PATIENCE_MS - 1]) {
      const s = derive({ status: last, noAnswerSince: NOW - gone, activeKnown: true });
      expect(s.kind).toBe("restarting");
    }
  });

  it("after 25 minutes without an answer the card says so", () => {
    const s = derive({ status: status(run({ phase: "up" })), noAnswerSince: NOW - RESTART_PATIENCE_MS, activeKnown: true });
    expect(s.kind).toBe("restartTimeout");
  });

  it("no answer without a run under way is not a restart", () => {
    const s = derive({ status: status(null), noAnswerSince: NOW - MIN, activeKnown: false });
    expect(s.kind).toBe("idle");
  });

  it("done → success (within a day, or watched by this page)", () => {
    const done = run({ phase: "done", running: false, finished_at: ago(5 * MIN) });
    expect(derive({ status: status(done) }).kind).toBe("success");
    const old = run({ phase: "done", running: false, finished_at: ago(3 * 24 * 60 * MIN) });
    expect(derive({ status: status(old) }).kind).toBe("idle");
    expect(derive({ status: status(old), seenActive: true }).kind).toBe("success");
  });

  it("rolled_back → set back, with the step it failed in", () => {
    const r = run({
      phase: "rolled_back",
      running: false,
      finished_at: ago(MIN),
      events: [
        { phase: "pull", event: "done" },
        { phase: "backup", event: "done" },
        { phase: "switch", event: "done" },
        { phase: "up", event: "done" },
        { phase: "verify", event: "intent" },
        { phase: "rolling_back", event: "intent" },
        { phase: "rolled_back", event: "done" },
      ],
    });
    const s = derive({ status: status(r) });
    expect(s.kind).toBe("rolledBack");
    expect(s.kind === "rolledBack" && s.step).toBe(4);
    expect(s.kind === "rolledBack" && s.requested).toBe(false);
  });

  it("a way back the operator asked for reads as requested", () => {
    const r = run({
      phase: "rolled_back",
      running: false,
      finished_at: ago(MIN),
      events: [{ phase: "done", event: "done" }, { phase: "rolling_back", event: "intent" }],
    });
    expect(wasRequested(r)).toBe(true);
  });

  it("interrupted (journal open, nobody holds the lock) → interrupted", () => {
    const s = derive({ status: status(run({ phase: "up", running: false, interrupted: true }), { command: "./install.sh --resume" }) });
    expect(s.kind).toBe("interrupted");
    expect(s.kind === "interrupted" && s.bridgeReachable).toBe(true);
  });

  it("bridge gone and the drain held → interrupted with the resume command", () => {
    const s = derive({
      status: status(run({ phase: "up", running: false }), { stale: true, bridge_reachable: false, command: "./install.sh --resume" }),
      staleSince: NOW,
    });
    expect(s).toMatchObject({ kind: "interrupted", bridgeReachable: false, command: "./install.sh --resume" });
  });

  it("the bridge away for a moment (restarted) does not end a running update", () => {
    const st = status(run({ phase: "pull", running: true }), { stale: true, bridge_reachable: false });
    expect(derive({ status: st, staleSince: NOW - 10_000 }).kind).toBe("running");
    expect(derive({ status: st, staleSince: NOW - STALE_GRACE_MS }).kind).toBe("interrupted");
  });

  it("rollback_failed → the way back failed, however old", () => {
    const r = run({ phase: "rollback_failed", running: false, finished_at: ago(5 * 24 * 60 * MIN) });
    expect(derive({ status: status(r) }).kind).toBe("rollbackFailed");
  });

  it("aborted → aborted (nothing changed)", () => {
    const r = run({ phase: "aborted", running: false, failure_kind: "network", finished_at: ago(MIN) });
    expect(derive({ status: status(r) }).kind).toBe("aborted");
  });

  it("rolling_back → rolling back, showing the step it failed in", () => {
    const r = run({ phase: "rolling_back", events: [{ phase: "up", event: "intent" }, { phase: "rolling_back", event: "intent" }] });
    const s = derive({ status: status(r) });
    expect(s.kind).toBe("rollingBack");
    expect(failedStep(r)).toBe(3);
  });

  it("just started: the previous run's result does not show until the new run does", () => {
    const old = run({ run_id: "20261018T100000Z-000001", phase: "done", running: false, finished_at: ago(MIN) });
    const s = derive({ status: status(old), expect: { runId: "20261019T120000Z-aaaaaa", action: "start" } });
    expect(s.kind).toBe("pending");
    expect(isActive(s)).toBe(true);
  });

  it("a way back is pending until its run leaves 'done'", () => {
    const done = run({ phase: "done", running: false, finished_at: ago(MIN) });
    const e = { runId: done.run_id, action: "rollback" as const };
    expect(expectArrived(e, done)).toBe(false);
    expect(expectArrived(e, { ...done, phase: "drain" })).toBe(true);
  });
});

describe("canGoBack — 'back to …' for 14 days after a success", () => {
  it("offers it for a recent success and not after 14 days", () => {
    expect(canGoBack(run({ phase: "done", finished_at: ago(13 * 24 * 60 * MIN) }), NOW)).toBe(true);
    expect(canGoBack(run({ phase: "done", finished_at: ago(15 * 24 * 60 * MIN) }), NOW)).toBe(false);
    expect(canGoBack(run({ phase: "rolled_back", finished_at: ago(MIN) }), NOW)).toBe(false);
  });
});
