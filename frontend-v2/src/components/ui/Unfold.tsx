"use client";

import { useEffect, useRef, useState, type ReactNode, type TransitionEvent } from "react";

/** Dauer des Auf-/Zuklappens (design-dna: Transitions 200-300 ms). Muss zur
 *  CSS-Klasse `.unfold` in globals.css passen — der Timer unten ist nur der
 *  Fallback, falls `transitionend` nie kommt (Tab im Hintergrund, Element
 *  mitten im Übergang aus dem Layout genommen). */
export const UNFOLD_MS = 240;

function motionAllowed(): boolean {
  // Kein matchMedia (jsdom, alte Engines) = keine Bewegung. So bleiben Tests
  // deterministisch, und ein Browser ohne Media-Queries klappt einfach sofort.
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") return false;
  return !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/** Faltet Inhalt auf und zu — per CSS-Grid `0fr → 1fr` (die einzige
 *  Höhen-Animation, die ohne Messen auskommt) plus opacity/transform am
 *  Inhalt. Geschlossen ist NICHTS gemountet (Screenreader, Suchen-im-Text,
 *  Tests sehen keinen versteckten Körper). Beim Schliessen bleibt der Inhalt
 *  für die Dauer des Übergangs stehen (`data-state="closing"`) und wird nach
 *  `transitionend` — spätestens nach dem Fallback-Timer — entfernt. */
export function Unfold({
  open,
  children,
  className,
}: {
  open: boolean;
  children: ReactNode;
  className?: string;
}) {
  // `mounted` hinkt `open` beim Schliessen hinterher; beim Öffnen ist es sofort
  // wahr, damit der Inhalt für den 0fr→1fr-Übergang schon im DOM steht.
  const [mounted, setMounted] = useState(open);
  // Erster Frame nach dem Mount noch bei 0fr, damit ein Übergang überhaupt
  // stattfindet (sonst springt der Browser direkt auf 1fr).
  const [expanded, setExpanded] = useState(open);
  const fallback = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (fallback.current) {
      clearTimeout(fallback.current);
      fallback.current = null;
    }
    if (open) {
      setMounted(true);
      if (!motionAllowed()) {
        setExpanded(true);
        return;
      }
      const raf = requestAnimationFrame(() => setExpanded(true));
      return () => cancelAnimationFrame(raf);
    }
    setExpanded(false);
    if (!motionAllowed()) {
      setMounted(false);
      return;
    }
    fallback.current = setTimeout(() => setMounted(false), UNFOLD_MS + 80);
    return () => {
      if (fallback.current) clearTimeout(fallback.current);
    };
  }, [open]);

  if (!mounted) return null;

  const state = open ? "open" : "closing";
  const onTransitionEnd = (e: TransitionEvent<HTMLDivElement>) => {
    if (e.target !== e.currentTarget || open) return;
    setMounted(false);
  };

  return (
    <div
      data-testid="unfold"
      data-state={state}
      className={`unfold${expanded && open ? " unfold-open" : ""}${className ? ` ${className}` : ""}`}
      onTransitionEnd={onTransitionEnd}
    >
      <div className="unfold-inner">{children}</div>
    </div>
  );
}
