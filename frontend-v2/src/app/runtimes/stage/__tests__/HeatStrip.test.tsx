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
  it("groups 3 points (5s each) into one 15s bucket, 60 buckets from 180 points", () => {
    const pts = points(180, () => 30);
    const buckets = bucketPulse(pts);
    expect(buckets).toHaveLength(60);
    expect(buckets.every((v) => v === 30)).toBe(true);
  });

  it("empty points → 60 zero buckets, never throws", () => {
    expect(bucketPulse(undefined)).toHaveLength(60);
    expect(bucketPulse([])).toEqual(new Array(60).fill(0));
  });

  // Live-Fund 06.09.2026 (Puls-Deploy): die Zeitachse läuft nach rechts — der
  // JÜNGSTE Punkt gehört immer in die letzte Zelle (Index 59 = "jetzt"),
  // fehlende Vorgeschichte bleibt links leer. Ein frisch gefüllter Ring
  // (wenige Punkte, noch nicht 180) zeigte die Aktivität vorher ganz links.
  it("right-aligns: a sparse ring's activity sits at the RIGHT end, not the left", () => {
    // 6 points, two clean 15s buckets (no fractional-bucket ambiguity):
    // oldest 3 quiet, newest 3 loud.
    const pts = points(6, (i) => (i < 3 ? 0 : 60));
    const buckets = bucketPulse(pts);
    expect(buckets).toHaveLength(60);
    // Everything left of the last two cells is empty — no history to show.
    expect(buckets.slice(0, 58).every((v) => v === 0)).toBe(true);
    expect(buckets[58]).toBe(0); // oldest bucket (points 0-2, all quiet)
    expect(buckets[59]).toBe(60); // newest bucket (points 3-5, all loud) = "now"
  });

  // Zweiter Live-Fund 06.09.2026 (nach dem Rechtsbündigkeits-Fix): Bucket-
  // Wert = MAX statt Mittel der 3 Punkte — ein kurzer Burst bleibt sichtbar,
  // statt in seinem 15s-Fenster mit ruhigeren Nachbarn verwässert zu werden.
  it("4 points (not a clean multiple of 3): a single loud sample survives undiluted via MAX, not averaged away by quiet neighbours", () => {
    const pts = points(4, (i) => (i === 3 ? 53.8 : 0));
    const buckets = bucketPulse(pts);
    // Grouping from the END: newest bucket = [points 1,2,3] (indices 1-3),
    // oldest leftover = [point 0] alone. MAX of [0,0,53.8] = 53.8 — the
    // burst is NOT averaged down by its two quiet neighbours.
    expect(buckets[59]).toBe(53.8);
    expect(buckets[58]).toBe(0);
    expect(buckets.slice(0, 58).every((v) => v === 0)).toBe(true);
  });

  it("Live-Beweis 06.09.2026: last12 = 0,0,0,16,33,31,31,32,32,31,34,32 → MAX per 15s bucket, not the mean", () => {
    const raw = [0, 0, 0, 16, 33, 31, 31, 32, 32, 31, 34, 32];
    const pts = points(raw.length, (i) => raw[i]);
    const buckets = bucketPulse(pts);
    // 4 buckets of 3, newest-first from the end: [31,34,32]→59, [32,32,31]→58,
    // [16,33,31]→57, [0,0,0]→56.
    expect(buckets[59]).toBe(34);
    expect(buckets[58]).toBe(32);
    expect(buckets[57]).toBe(33);
    expect(buckets[56]).toBe(0);
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

  // Live-Fund 06.09.2026: Helligkeit skaliert auf max(50, beobachtetes
  // Maximum) statt einer festen 50-tok/s-Decke — sonst klemmt ein Rezept,
  // das dauerhaft über 50 tok/s läuft, JEDE Zelle auf volle Helligkeit und
  // verliert die Unterscheidung zwischen z.B. 60 und 90 tok/s.
  it("accepts a custom ceiling — a value at the ceiling is always full, below it scales down", () => {
    expect(cellOpacity(90, false, 90)).toBe(1);
    expect(cellOpacity(45, false, 90)).toBeCloseTo(0.08 + 0.92 * Math.sqrt(0.5));
  });

  it("defaults to the 50 tok/s ceiling when none is given (no regression)", () => {
    expect(cellOpacity(25, false)).toBeCloseTo(0.08 + 0.92 * Math.sqrt(0.5));
  });

  // Zweiter Live-Fund 06.09.2026: 32 tok/s auf einer 50er-Decke (64% Last)
  // wirkte "mittelgrau, halb tot" mit der linearen Kurve (0,08+0,92·0,64 =
  // 0,67). Die Wurzelkurve hebt mittlere Lasten sichtbar an: 64% Last →
  // ~92% Helligkeit statt 67%.
  it("root curve lifts medium load noticeably brighter than the old linear formula would", () => {
    const ceiling = 50;
    const load64 = cellOpacity(32, false, ceiling);
    const linearWouldHaveBeen = 0.08 + 0.92 * (32 / ceiling);
    expect(load64).toBeGreaterThan(linearWouldHaveBeen);
    expect(load64).toBeCloseTo(0.08 + 0.92 * Math.sqrt(32 / ceiling));
  });

  it("zero stays at the floor even under the root curve (sqrt(0) = 0)", () => {
    expect(cellOpacity(0, false, 50)).toBeCloseTo(0.08);
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

  it("the newest activity renders at the RIGHT end of the strip, not the left (Live-Fund 06.09.2026)", () => {
    const pulse: HostPulse = { points: points(6, (i) => (i < 3 ? 0 : 60)), now_tps: 60, idle_seconds: 0, available: true };
    render(<HeatStrip pulse={pulse} />);
    const cells = Array.from(screen.getByTestId("heat-strip").children) as HTMLElement[];
    // Left of the last two cells: floor opacity (bg-hover-equivalent 0 tok/s).
    expect(cells.slice(0, 58).every((c) => parseFloat(c.style.opacity) < 0.1)).toBe(true);
    // The last cell (index 59, "now") is the loud one — full opacity, since
    // its own bucket value (60) is also this render's observed max.
    expect(parseFloat(cells[59].style.opacity)).toBe(1);
  });

  it("scales opacity to the observed max when it exceeds the 50 tok/s default (a >50 tok/s recipe keeps differentiation)", () => {
    // Two buckets: an 80 tok/s burst (this render's max) and a 40 tok/s one.
    // Under a FIXED 50-ceiling both would clip toward/at full brightness;
    // scaling to the observed max (80) keeps 40 visibly dimmer than 80.
    const pts = points(6, (i) => (i < 3 ? 40 : 80));
    const pulse: HostPulse = { points: pts, now_tps: 80, idle_seconds: 0, available: true };
    render(<HeatStrip pulse={pulse} />);
    const cells = Array.from(screen.getByTestId("heat-strip").children) as HTMLElement[];
    const dimmer = parseFloat(cells[58].style.opacity); // 40 tok/s bucket
    const brightest = parseFloat(cells[59].style.opacity); // 80 tok/s bucket
    expect(brightest).toBe(1);
    expect(dimmer).toBeLessThan(brightest);
    expect(dimmer).toBeCloseTo(0.08 + 0.92 * Math.sqrt(40 / 80));
  });
});
