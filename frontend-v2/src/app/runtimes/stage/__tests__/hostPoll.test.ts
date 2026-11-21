import { describe, it, expect } from "vitest";
import { QueryClient } from "@tanstack/react-query";
import { hostPollMs, metricsPoll, pulsePoll, HOST_POLL_AWAKE_MS, HOST_POLL_ASLEEP_MS } from "../hostPoll";
import type { HostMetrics } from "@/lib/types";

const m = (over: Partial<HostMetrics>) => ({ reachable: true, ...over }) as HostMetrics;

describe("host polling slows down for a box that does not answer", () => {
  it("polls an answering box every 5 s", () => {
    expect(HOST_POLL_AWAKE_MS).toBe(5_000);
    expect(hostPollMs(m({ reachable: true, status: "serving" }))).toBe(5_000);
  });

  it("polls a sleeping or unreachable box once a minute", () => {
    expect(HOST_POLL_ASLEEP_MS).toBe(60_000);
    expect(hostPollMs(m({ reachable: false }))).toBe(60_000);
    expect(hostPollMs(m({ reachable: false, status: "asleep" }))).toBe(60_000);
    expect(hostPollMs(m({ reachable: true, status: "asleep" }))).toBe(60_000);
  });

  it("keeps 5 s before the first answer (nothing known yet)", () => {
    expect(hostPollMs(undefined)).toBe(5_000);
  });

  it("metricsPoll reads the query's own data", () => {
    expect(metricsPoll({ state: { data: m({ reachable: false }) } })).toBe(60_000);
    expect(metricsPoll({ state: { data: m({ reachable: true }) } })).toBe(5_000);
  });

  it("pulsePoll follows the head box's metrics in the cache", () => {
    const qc = new QueryClient();
    qc.setQueryData(["hosts", "h1", "metrics"], m({ reachable: false, status: "asleep" }));
    expect(pulsePoll(qc, "h1")()).toBe(60_000);
    qc.setQueryData(["hosts", "h1", "metrics"], m({ reachable: true }));
    expect(pulsePoll(qc, "h1")()).toBe(5_000);
    expect(pulsePoll(qc, undefined)()).toBe(5_000);
  });
});
