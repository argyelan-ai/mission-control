/**
 * One agent state instead of three chips (round-2 user test t4 S5/S-neu-2).
 *
 * A local agent showed "LIVE · PAUSED · Working" at once, the dot next to
 * "Working" grey: the provisioning chip, the operational mode and the raw
 * heartbeat status, each on its own. `agentStatusView` folds them into ONE
 * state with one dot and one translated label. "Paused" in MC means "gets no
 * new tasks" (dispatch gate R-paused-no-dispatch) — a running task goes on,
 * and the label says exactly that.
 */
import { describe, it, expect } from "vitest";
import { agentStatusView, isAgentWorking } from "../agentStatus";
import { formatContextPct } from "../utils";

describe("agentStatusView", () => {
  it("a working agent is busy — the heartbeat says 'working', not 'busy'", () => {
    expect(isAgentWorking("working")).toBe(true);
    expect(isAgentWorking("busy")).toBe(true);
    expect(isAgentWorking("idle")).toBe(false);
    expect(agentStatusView({ status: "working", operational_mode: "active" })).toEqual({
      dot: "busy", labelKey: "status.working", paused: false, working: true,
    });
  });

  it("paused and still working: one state that says the task finishes", () => {
    expect(agentStatusView({ status: "working", operational_mode: "paused" })).toEqual({
      dot: "busy", labelKey: "status.pausedWorking", paused: true, working: true,
    });
  });

  it("paused and idle: one state that says no new tasks", () => {
    expect(agentStatusView({ status: "idle", operational_mode: "paused" })).toEqual({
      dot: "warning", labelKey: "status.paused", paused: true, working: false,
    });
  });

  it("maps every heartbeat status to a translated label and a dot", () => {
    const cases: Array<[string, string, string]> = [
      ["online", "online", "status.online"],
      ["idle", "idle", "status.idle"],
      ["error", "error", "status.error"],
      ["restarting", "warning", "status.restarting"],
      ["provisioning", "warning", "status.provisioning"],
      ["offline", "offline", "status.offline"],
      ["something-new", "offline", "status.offline"],
    ];
    for (const [status, dot, labelKey] of cases) {
      expect(agentStatusView({ status, operational_mode: "active" })).toMatchObject({ dot, labelKey });
    }
  });
});

describe("formatContextPct", () => {
  it("never shows 0 % for a context that holds tokens", () => {
    // 4,200 of 1,048,576 tokens = 0.4 % — "0 %" read as "nothing in it".
    expect(formatContextPct(4_200, 1_048_576)).toBe("<1%");
  });
  it("rounds normal values and keeps a real zero and unknown apart", () => {
    expect(formatContextPct(20_972, 1_048_576)).toBe("2%");
    expect(formatContextPct(0, 1_048_576)).toBe("0%");
    expect(formatContextPct(null, 1_048_576)).toBe("—");
    expect(formatContextPct(10, 0)).toBe("—");
  });
});
