"use client";

/**
 * FlowEdge — das „Lauflicht" um die Kartenkante (Spec §2 Zone 0).
 *
 * Vier SVG-Rect-Lagen (Deckung 1/.5/.25/.1) ziehen als heller Kopf mit
 * ausblendendem Schweif um den Kartenrand. Bewegung läuft über rAF-JS statt
 * CSS `stroke-dashoffset` + `pathLength` — Safari rendert `pathLength` auf
 * einem `<rect>` inkonsistent, ein JS-getriebener `stroke-dashoffset` ist
 * überall gleich.
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

export function FlowEdge({ kind }: { kind: FlowKind }) {
  const reduced = useReducedMotionPref();
  const containerRef = useRef<SVGSVGElement>(null);
  const [visible, setVisible] = useState(false);
  const [size, setSize] = useState({ w: 0, h: 0 });
  const rafRef = useRef<number | null>(null);
  const offsetRef = useRef(0);
  const [, bump] = useState(0);

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
    const ro = new ResizeObserver(() => {
      const rect = parent.getBoundingClientRect();
      setSize({ w: rect.width, h: rect.height });
    });
    ro.observe(parent);
    return () => ro.disconnect();
  }, []);

  const duration = DURATION_S[kind];
  const shouldAnimate = visible && !reduced && duration > 0 && size.w > 0 && size.h > 0;

  useEffect(() => {
    if (!shouldAnimate) {
      if (rafRef.current != null) cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
      return;
    }
    const perimeter = 2 * (size.w + size.h);
    let start: number | null = null;
    const tick = (ts: number) => {
      if (start === null) start = ts;
      const elapsedS = (ts - start) / 1000;
      offsetRef.current = ((elapsedS / duration) % 1) * perimeter;
      bump((n) => (n + 1) % 1_000_000);
      rafRef.current = requestAnimationFrame(tick);
    };
    rafRef.current = requestAnimationFrame(tick);
    return () => {
      if (rafRef.current != null) cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shouldAnimate, duration, size.w, size.h]);

  const strokeColor =
    kind === "dead" ? "var(--color-error)" : kind === "dim" ? "var(--color-warning)" : "var(--color-accent)";

  const perimeter = 2 * (size.w + size.h);

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
        LAYER_OPACITY.map((opacity, i) => {
          const dashLen = Math.max(1, perimeter * LAYER_LENGTH_FRAC[i]);
          const dashOffset = shouldAnimate ? -offsetRef.current : 0;
          return (
            <rect
              key={i}
              x={0.75}
              y={0.75}
              width={Math.max(0, size.w - 1.5)}
              height={Math.max(0, size.h - 1.5)}
              rx={8}
              fill="none"
              stroke={strokeColor}
              strokeWidth={1.5}
              strokeDasharray={kind === "dead" ? `3 4` : `${dashLen} ${Math.max(0, perimeter - dashLen)}`}
              strokeDashoffset={dashOffset}
              opacity={kind === "dead" ? opacity * 0.5 : opacity}
              strokeLinecap="butt"
            />
          );
        })}
    </svg>
  );
}
