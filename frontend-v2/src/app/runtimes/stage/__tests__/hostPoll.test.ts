import { describe, it, expect } from "vitest";
import { QueryClient } from "@tanstack/react-query";
import {
  hostPollMs,
  metricsPoll,
  pulsePoll,
  HOST_POLL_AWAKE_MS,
  HOST_POLL_ASLEEP_MS,
  HOST_POLL_UNREACHABLE_MS,
} from "../hostPoll";
import type { HostMetrics } from "@/lib/types";

const m = (over: Partial<HostMetrics>) => ({ reachable: true, ...over }) as HostMetrics;

describe("host polling slows down for a box that does not answer", () => {
  it("polls an answering box every 5 s", () => {
    expect(HOST_POLL_AWAKE_MS).toBe(5_000);
    expect(hostPollMs(m({ reachable: true, status: "serving" }))).toBe(5_000);
  });

  it("polls a box that says it is asleep once a minute", () => {
    expect(HOST_POLL_ASLEEP_MS).toBe(60_000);
    expect(hostPollMs(m({ reachable: false, status: "asleep" }))).toBe(60_000);
    expect(hostPollMs(m({ reachable: true, status: "asleep" }))).toBe(60_000);
  });

  // Not answering without "asleep" is a reboot or a network flap: it comes
  // back by itself, outside MC's wake button, so the stage looks sooner.
  it("polls an unreachable box (reboot, flap) every 20 s", () => {
    expect(HOST_POLL_UNREACHABLE_MS).toBe(20_000);
    expect(hostPollMs(m({ reachable: false }))).toBe(20_000);
    expect(hostPollMs(m({ reachable: false, status: "error" }))).toBe(20_000);
  });

  it("keeps 5 s before the first answer (nothing known yet)", () => {
    expect(hostPollMs(undefined)).toBe(5_000);
  });

  it("metricsPoll reads the query's own data", () => {
    expect(metricsPoll({ state: { data: m({ reachable: false, status: "asleep" }) } })).toBe(60_000);
    expect(metricsPoll({ state: { data: m({ reachable: false }) } })).toBe(20_000);
    expect(metricsPoll({ state: { data: m({ reachable: true }) } })).toBe(5_000);
  });

  it("pulsePoll follows the head box's metrics in the cache", () => {
    const qc = new QueryClient();
    qc.setQueryData(["hosts", "h1", "metrics"], m({ reachable: false, status: "asleep" }));
    expect(pulsePoll(qc, "h1")()).toBe(60_000);
    qc.setQueryData(["hosts", "h1", "metrics"], m({ reachable: false }));
    expect(pulsePoll(qc, "h1")()).toBe(20_000);
    qc.setQueryData(["hosts", "h1", "metrics"], m({ reachable: true }));
    expect(pulsePoll(qc, "h1")()).toBe(5_000);
    expect(pulsePoll(qc, undefined)()).toBe(5_000);
  });
});

describe("every metrics/pulse query on the stage uses the box-aware interval", () => {
  // One observer with a fixed 5 s keeps its own timer and polls a sleeping
  // box every 5 s again, whatever the others say.
  it("no stage file polls host metrics or pulse at a fixed interval", async () => {
    const { readdirSync, readFileSync, statSync } = await import("node:fs");
    const path = await import("node:path");
    const root = path.resolve(__dirname, "..");
    const files: string[] = [];
    const walk = (dir: string) => {
      for (const f of readdirSync(dir)) {
        const p = path.join(dir, f);
        if (statSync(p).isDirectory()) { if (f !== "__tests__") walk(p); }
        else if (/\.tsx?$/.test(f)) files.push(p);
      }
    };
    walk(root);
    const offenders: string[] = [];
    for (const file of files) {
      const src = readFileSync(file, "utf8");
      const re = /queryKey:\s*\[\s*"hosts",[^\]]*"(metrics|pulse)"\s*\],[\s\S]{0,300}?refetchInterval:\s*([^,\n]+)/g;
      for (const m of src.matchAll(re)) {
        if (/^\d/.test(m[2].trim())) offenders.push(`${path.basename(file)}: ${m[1]} ${m[2].trim()}`);
      }
    }
    expect(offenders).toEqual([]);
  });
});
