"use client";

/**
 * HeatStrip — der Wärmestreifen unter dem Lebenszeichen (Spec §2 Zone 1).
 *
 * 60 Zellen à 15 s = 15 min. Helligkeit (Opacity .08–1) = Token/s. Baut die
 * Zellen aus dem Puls-Ring (GET /hosts/{id}/pulse, §6) — 180 Punkte in 5-s-
 * Schritten werden zu 60 Buckets à 3 Punkte zusammengefasst (3×5s=15s).
 *
 * Ohne Puls (available:false, z.B. Endpoint noch nicht deployt) sind alle
 * Zellen leer (bg-hover) statt eines Fehlers — HONESTY RULE, kein Banner für
 * eine fehlende Metrik.
 */

import { C } from "@/lib/colors";
import type { HostPulse } from "@/lib/types";

const CELLS = 60;
const POINTS_PER_CELL = 3; // 180 Punkte / 60 Zellen
// tok/s, ab der eine Zelle als "voll hell" gilt — grobe Kalibrierung analog
// dem Mockup (mx=50 dort); reale Rezepte laufen typischerweise 15-60 tok/s.
const MAX_TPS = 50;

export function bucketPulse(points: HostPulse["points"] | undefined): number[] {
  const pts = points ?? [];
  const buckets: number[] = [];
  for (let i = 0; i < CELLS; i++) {
    const slice = pts.slice(i * POINTS_PER_CELL, i * POINTS_PER_CELL + POINTS_PER_CELL);
    if (slice.length === 0) {
      buckets.push(0);
      continue;
    }
    const avg = slice.reduce((sum, p) => sum + (p.tps ?? 0), 0) / slice.length;
    buckets.push(avg);
  }
  return buckets;
}

export function cellOpacity(tps: number, dead: boolean): number {
  if (dead) return 0.35;
  return Math.min(1, 0.08 + 0.92 * Math.min(1, tps / MAX_TPS));
}

export function HeatStrip({
  pulse,
  dead = false,
  empty = false,
}: {
  pulse?: HostPulse;
  /** Störung: alle Zellen rot statt Akzent-Farbe (Spec §2 Zone 1 "Tot"). */
  dead?: boolean;
  /** Freie Karte: 60 leere Zellen (bg-hover), kein Akzent — kein "Puls" ohne Modell. */
  empty?: boolean;
}) {
  const available = pulse?.available === true;
  const buckets = empty || !available ? new Array(CELLS).fill(0) : bucketPulse(pulse?.points);

  return (
    <div
      className="grid gap-0.5 mt-2"
      style={{ gridTemplateColumns: `repeat(${CELLS}, 1fr)`, height: "14px" }}
      data-testid="heat-strip"
      aria-hidden="true"
    >
      {buckets.map((tps, i) => (
        <div
          key={i}
          className="rounded-[1px]"
          style={{
            background: empty || !available ? C.bgHover : dead ? C.error : C.accent,
            opacity: empty || !available ? 1 : cellOpacity(tps, dead),
          }}
        />
      ))}
    </div>
  );
}
