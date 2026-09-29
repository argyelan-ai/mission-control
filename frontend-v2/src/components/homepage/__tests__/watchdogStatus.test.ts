import { describe, expect, it } from "vitest";
import { watchdogView } from "../watchdogStatus";

describe("watchdogView", () => {
  it("running in the worker process is green and says so", () => {
    const v = watchdogView({ status: "running", source: "worker", checks_total: 12 });
    expect(v.dot).toBe("ok");
    expect(v.key).toBe("watchdogRunningWorker");
    expect(v.values).toEqual({ count: 12 });
    expect(v.isError).toBe(false);
  });

  it("running in this process shows the check count only", () => {
    const v = watchdogView({ status: "running", source: "local", checks_total: 3 });
    expect(v.dot).toBe("ok");
    expect(v.key).toBe("checks");
  });

  it("a stale heartbeat is a warning with its age, not green", () => {
    const v = watchdogView({ status: "stale", source: "worker", last_seen: "2026-01-01T00:00:00Z" });
    expect(v.dot).toBe("warning");
    expect(v.key).toBe("watchdogStale");
    expect(v.lastSeen).toBe("2026-01-01T00:00:00Z");
    expect(v.isError).toBe(false);
  });

  it("stopped is an error, not a grey detail", () => {
    const v = watchdogView({ status: "stopped", source: null });
    expect(v.dot).toBe("down");
    expect(v.key).toBe("watchdogStopped");
    expect(v.isError).toBe(true);
  });

  it("missing component reads as unknown", () => {
    const v = watchdogView(undefined);
    expect(v.dot).toBe("unknown");
    expect(v.key).toBe("watchdogUnknown");
  });
});
