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
 *
 * Bucket-Wert = MAX statt Mittel der 3 Punkte (zweiter Live-Fund
 * 06.09.2026, nach dem Rechtsbündigkeits-Fix): ein kurzer Burst bleibt so
 * sichtbar, statt in seinem 15s-Fenster mit Nullen/ruhigeren Nachbarn
 * verwässert zu werden — Beispiel aus dem Live-Beweis (12 Rohwerte)
 * `0,0,0,16,33,31,31,32,32,31,34,32`: ein Mittel-Bucket würde den
 * Anstieg abflachen, MAX zeigt ihn.
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
    const max = slice.reduce((m, p) => Math.max(m, p.tps ?? 0), 0);
    buckets[CELLS - 1 - b] = max;
  }
  return buckets;
}

/**
 * `ceiling` (Live-Fund 06.09.2026): skaliert die Helligkeit auf
 * `max(50, beobachtetes Maximum der aktuellen Zellen)` statt einer festen
 * 50-tok/s-Decke — ein Rezept, das dauerhaft schneller als 50 tok/s läuft,
 * bekäme sonst JEDE Zelle auf volle Helligkeit geklemmt und verlöre die
 * Unterscheidung zwischen einem 60er- und einem 90er-Burst.
 *
 * Wurzelkurve statt linear (zweiter Live-Fund 06.09.2026): bei 32 tok/s auf
 * einer 50er-Decke wirkte die Zelle "mittelgrau, halb tot" — linear
 * (0,08+0,92·v/decke) liefert bei 64 % Last nur 64 % Helligkeit. `sqrt`
 * hebt mittlere Lasten sichtbar an (64 % Last → 80 % Helligkeit), Nullen
 * bleiben bei der Bodenopazität .08.
 */
export function cellOpacity(tps: number, dead: boolean, ceiling: number = MAX_TPS): number {
  if (dead) return 0.35;
  return Math.min(1, 0.08 + 0.92 * Math.sqrt(Math.min(1, tps / ceiling)));
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
