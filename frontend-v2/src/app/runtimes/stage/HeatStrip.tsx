"use client";

/**
 * HeatStrip — der Wärmestreifen unter dem Lebenszeichen (Spec §2 Zone 1).
 *
 * 60 Zellen à 15 s = 15 min. Helligkeit (Opacity .08–1) = Token/s, Decke
 * dynamisch auf max(50, beobachtetes Maximum). Baut die Zellen aus dem
 * Puls-Ring (GET /hosts/{id}/pulse, §6) — 180 Punkte in 5-s-Schritten werden
 * RECHTSBÜNDIG zu 60 Buckets à 3 Punkte zusammengefasst (3×5s=15s): die
 * letzte Zelle (Index 59) ist immer "jetzt", fehlende Vorgeschichte bleibt
 * links leer (Live-Fund 06.09.2026, Puls-Deploy — vorher links-bündig, der
 * Burst landete am falschen Rand).
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

/**
 * Rechtsbündig (Live-Fund 06.09.2026, Puls-Deploy): die Zeitachse läuft nach
 * rechts — der LETZTE (jüngste) Punkt gehört immer in die letzte Zelle
 * (Index 59 = "jetzt"), ältere Punkte wandern nach links, fehlende
 * Vorgeschichte bleibt links leer. Vorher gruppierte `bucketPulse` von
 * Index 0 aus — bei einem frisch gefüllten Ring (noch keine 180 Punkte)
 * landete die einzige echte Aktivität dadurch ganz LINKS statt rechts.
 * Gruppierung bleibt 3 Punkte (15s) pro Zelle, jetzt vom Ende der Liste aus
 * rückwärts gezählt statt vom Anfang.
 */
export function bucketPulse(points: HostPulse["points"] | undefined): number[] {
  const pts = points ?? [];
  const buckets: number[] = new Array(CELLS).fill(0);
  const totalBuckets = Math.ceil(pts.length / POINTS_PER_CELL);
  for (let b = 0; b < Math.min(totalBuckets, CELLS); b++) {
    const end = pts.length - b * POINTS_PER_CELL;
    const start = Math.max(0, end - POINTS_PER_CELL);
    const slice = pts.slice(start, end);
    if (slice.length === 0) continue;
    const avg = slice.reduce((sum, p) => sum + (p.tps ?? 0), 0) / slice.length;
    buckets[CELLS - 1 - b] = avg;
  }
  return buckets;
}

/**
 * `ceiling` (Live-Fund 06.09.2026): Live-Beweis zeigte 53,8 tok/s als
 * "mittelgrau" statt voll hell — Root Cause war die Links-Ausrichtung oben
 * (der Burst landete in einer mit älteren Nahe-Null-Punkten gemittelten
 * Zelle). Zusätzlich zur Rechts-Ausrichtung skaliert die Helligkeit jetzt auf
 * `max(50, beobachtetes Maximum der aktuellen Zellen)` statt einer festen
 * 50-tok/s-Decke — ein Rezept, das dauerhaft schneller als 50 tok/s läuft,
 * bekäme sonst JEDE Zelle auf volle Helligkeit geklemmt und verlöre die
 * Unterscheidung zwischen einem 60er- und einem 90er-Burst.
 */
export function cellOpacity(tps: number, dead: boolean, ceiling: number = MAX_TPS): number {
  if (dead) return 0.35;
  return Math.min(1, 0.08 + 0.92 * Math.min(1, tps / ceiling));
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
  const ceiling = empty || !available ? MAX_TPS : Math.max(MAX_TPS, ...buckets);

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
            opacity: empty || !available ? 1 : cellOpacity(tps, dead, ceiling),
          }}
        />
      ))}
    </div>
  );
}
