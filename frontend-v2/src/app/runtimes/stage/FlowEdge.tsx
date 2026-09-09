"use client";

/**
 * FlowEdge — das „Lauflicht" um die Kartenkante (Spec §2 Zone 0).
 *
 * Vier SVG-Rect-Lagen (Deckung 1/.5/.25/.1) ziehen als heller Kopf mit
 * ausblendendem Schweif um den Kartenrand. Bewegung läuft über rAF-JS statt
 * CSS `stroke-dashoffset` + `pathLength` — Safari rendert `pathLength` auf
 * einem `<rect>` inkonsistent, ein JS-getriebener `stroke-dashoffset` ist
 * überall gleich. Der rAF-Tick schreibt die Offsets direkt in die Rects
 * (Refs) — kein React-Re-Render pro Frame.
 *
 * Geometrie (Nachlese 09.09.):
 *  - Grösse = Layout-Grösse aus dem ResizeObserver (`contentRect`), NICHT
 *    `getBoundingClientRect()`: die ist transform-behaftet und lieferte
 *    während des framer-motion-Layout-Übergangs (scaleY) eine gestauchte
 *    Höhe, die dann für immer stehen blieb.
 *  - Eckradius folgt dem `border-radius` der Karte (konzentrisch, der Strich
 *    liegt 1.75 px innerhalb der Aussenkante).
 *  - Muster-Periode = echte Pfadlänge des abgerundeten Rechtecks, sonst
 *    setzt das Licht einmal pro Runde aus.
 *  - Alle Lagen enden am Kopf → der Schweif liegt hinter dem Kopf.
 *
 * Nur wenn sichtbar (IntersectionObserver) und nur wenn die Karte "live" ist
 * (serving/switching) läuft die Animation — `prefers-reduced-motion` hält
 * die Lagen still und gedimmt, eine gestoppte/leere/blaupausene Karte bekommt
 * gar kein FlowEdge (die aufrufende Stage rendert es dann einfach nicht).
 */

import { useEffect, useRef, useState } from "react";

export type FlowKind = "live" | "quiet" | "dim" | "dead";

// Sekunden pro Runde je Zustand (Spec §2 Zone 0).
const DURATION_S: Record<FlowKind, number> = {
  live: 7,
  quiet: 16,
  dim: 16,
  dead: 0, // still — kein Loop
};

const LAYER_OPACITY = [1, 0.5, 0.25, 0.1];
// Schweiflänge je Lage als Bruchteil des Umfangs.
const LAYER_LENGTH_FRAC = [0.02, 0.05, 0.08, 0.12];

// Abstand Strich-Mitte zur SVG-Kante; die SVG sitzt in der Padding-Box, also
// 1 px (Kartenrand) weiter innen als die Aussenkante.
const STROKE_INSET = 0.75;
const CARD_BORDER = 1;
const FALLBACK_RADIUS = 8;

/** Pfadlänge eines Rechtecks mit abgerundeten Ecken (Radius geklemmt auf die halbe kurze Seite). */
export function roundedRectPerimeter(w: number, h: number, r: number): number {
  const rr = Math.max(0, Math.min(r, w / 2, h / 2));
  return 2 * (w + h) - 8 * rr + 2 * Math.PI * rr;
}

/**
 * `stroke-dashoffset` je Lage, so dass jede Lage am Kopf (Lage 0) ENDET.
 * Lage i belegt [head + len0 − len_i, head + len0]; Sichtbereich beginnt bei −offset.
 */
export function layerDashOffsets(head: number, lens: number[]): number[] {
  return lens.map((len) => -(head + lens[0] - len));
}

function useReducedMotionPref(): boolean {
  const [reduced, setReduced] = useState(false);
  useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return;
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    setReduced(mq.matches);
    const onChange = () => setReduced(mq.matches);
    mq.addEventListener?.("change", onChange);
    return () => mq.removeEventListener?.("change", onChange);
  }, []);
  return reduced;
}

function readCardRadius(el: Element | null): number {
  if (!el || typeof getComputedStyle === "undefined") return FALLBACK_RADIUS;
  const raw = parseFloat(getComputedStyle(el).borderRadius);
  return Number.isFinite(raw) ? raw : FALLBACK_RADIUS;
}

export function FlowEdge({ kind }: { kind: FlowKind }) {
  const reduced = useReducedMotionPref();
  const containerRef = useRef<SVGSVGElement>(null);
  const rectRefs = useRef<(SVGRectElement | null)[]>([]);
  const [visible, setVisible] = useState(false);
  const [size, setSize] = useState({ w: 0, h: 0 });
  const [radius, setRadius] = useState(FALLBACK_RADIUS);
  const rafRef = useRef<number | null>(null);

  useEffect(() => {
    const el = containerRef.current;
    if (!el || typeof IntersectionObserver === "undefined") {
      setVisible(true);
      return;
    }
    const obs = new IntersectionObserver(([entry]) => setVisible(entry.isIntersecting));
    obs.observe(el);
    return () => obs.disconnect();
  }, []);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const parent = el.parentElement;
    if (!parent || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(([entry]) => {
      // Layout-Grösse — transform-unabhängig (siehe Kopfkommentar).
      setSize({ w: entry.contentRect.width, h: entry.contentRect.height });
      setRadius(readCardRadius(parent));
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const duration = DURATION_S[kind];
  const shouldAnimate = visible && !reduced && duration > 0 && size.w > 0 && size.h > 0;

  const rectW = Math.max(0, size.w - 2 * STROKE_INSET);
  const rectH = Math.max(0, size.h - 2 * STROKE_INSET);
  const rx = Math.max(0, radius - CARD_BORDER - STROKE_INSET);
  const perimeter = roundedRectPerimeter(rectW, rectH, rx);
  const dashLens = LAYER_LENGTH_FRAC.map((f) => Math.max(1, perimeter * f));

  useEffect(() => {
    if (!shouldAnimate) {
      if (rafRef.current != null) cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
      rectRefs.current.forEach((r) => r?.setAttribute("stroke-dashoffset", "0"));
      return;
    }
    let start: number | null = null;
    const tick = (ts: number) => {
      if (start === null) start = ts;
      const elapsedS = (ts - start) / 1000;
      const head = ((elapsedS / duration) % 1) * perimeter;
      const offsets = layerDashOffsets(head, dashLens);
      rectRefs.current.forEach((r, i) => r?.setAttribute("stroke-dashoffset", String(offsets[i])));
      rafRef.current = requestAnimationFrame(tick);
    };
    rafRef.current = requestAnimationFrame(tick);
    return () => {
      if (rafRef.current != null) cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shouldAnimate, duration, perimeter]);

  const strokeColor =
    kind === "dead" ? "var(--color-error)" : kind === "dim" ? "var(--color-warning)" : "var(--color-accent)";

  return (
    <svg
      ref={containerRef}
      aria-hidden="true"
      className="pointer-events-none absolute inset-0 w-full h-full"
      style={{ zIndex: 1, overflow: "visible" }}
      data-testid="flow-edge"
      data-kind={kind}
      data-animating={shouldAnimate ? "true" : "false"}
    >
      {size.w > 0 &&
        size.h > 0 &&
        LAYER_OPACITY.map((opacity, i) => (
          <rect
            key={i}
            ref={(node) => {
              rectRefs.current[i] = node;
            }}
            x={STROKE_INSET}
            y={STROKE_INSET}
            width={rectW}
            height={rectH}
            rx={rx}
            fill="none"
            stroke={strokeColor}
            strokeWidth={1.5}
            strokeDasharray={kind === "dead" ? `3 4` : `${dashLens[i]} ${Math.max(0, perimeter - dashLens[i])}`}
            strokeDashoffset={0}
            opacity={kind === "dead" ? opacity * 0.5 : opacity}
            strokeLinecap="butt"
          />
        ))}
    </svg>
  );
}
