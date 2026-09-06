/**
 * HeatStrip — 60-Zellen-Bucketing aus dem Puls-Ring (Spec §2 Zone 1, §6).
 */
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { HeatStrip, bucketPulse, cellOpacity } from "../HeatStrip";
import type { HostPulse } from "@/lib/types";

function points(n: number, tps: (i: number) => number): HostPulse["points"] {
  return Array.from({ length: n }, (_, i) => ({ t: i * 5, tps: tps(i) }));
}

describe("bucketPulse", () => {
  it("averages 3 points (5s each) into one 15s bucket, 60 buckets from 180 points", () => {
    const pts = points(180, () => 30);
    const buckets = bucketPulse(pts);
    expect(buckets).toHaveLength(60);
    expect(buckets.every((v) => v === 30)).toBe(true);
  });

  it("empty points → 60 zero buckets, never throws", () => {
    expect(bucketPulse(undefined)).toHaveLength(60);
    expect(bucketPulse([])).toEqual(new Array(60).fill(0));
  });
});

describe("cellOpacity", () => {
  it("dead → fixed dim opacity regardless of tps", () => {
    expect(cellOpacity(50, true)).toBe(0.35);
    expect(cellOpacity(0, true)).toBe(0.35);
  });

  it("0 tok/s → the floor opacity (never fully invisible)", () => {
    expect(cellOpacity(0, false)).toBeCloseTo(0.08);
  });

  it("at/above the calibration ceiling → full opacity, never over 1", () => {
    expect(cellOpacity(50, false)).toBe(1);
    expect(cellOpacity(500, false)).toBe(1);
  });
});

describe("HeatStrip (render)", () => {
  it("renders 60 cells, empty (no styling asserted beyond count) when available:false", () => {
    render(<HeatStrip pulse={{ points: [], now_tps: null, idle_seconds: null, available: false }} />);
    const strip = screen.getByTestId("heat-strip");
    expect(strip.children).toHaveLength(60);
  });

  it("empty free-box mode renders 60 cells too", () => {
    render(<HeatStrip empty />);
    expect(screen.getByTestId("heat-strip").children).toHaveLength(60);
  });

  it("dead (Störung) renders 60 cells with the dim opacity applied", () => {
    const pulse: HostPulse = { points: points(180, () => 40), now_tps: 40, idle_seconds: 0, available: true };
    render(<HeatStrip pulse={pulse} dead />);
    const strip = screen.getByTestId("heat-strip");
    expect(strip.children).toHaveLength(60);
    expect((strip.children[0] as HTMLElement).style.opacity).toBe("0.35");
  });
});
