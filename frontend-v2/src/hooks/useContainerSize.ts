"use client";

import { useLayoutEffect, useRef, useState, type RefObject } from "react";

export interface ContainerSize {
  width: number;
  height: number;
}

/**
 * Tracks the live pixel size of a DOM element via ResizeObserver.
 *
 * Root cause this exists for (Task #13, iOS-PWA memory graph bug):
 * iOS PWA (Home-Screen app, display-mode: standalone) reports
 * window.innerHeight, visualViewport.height, and CSS dvh/svh ALL smaller
 * than the real viewport (measured 793px vs the true 852px — only lvh is
 * trustworthy; see the app-shell's `@media (display-mode: standalone) {
 * height: 100lvh }` fix). Any component that sizes itself off
 * window.innerWidth/innerHeight inherits that lie.
 *
 * `react-force-graph-2d` (via the underlying `force-graph` lib) defaults its
 * canvas width/height to `window.innerWidth`/`window.innerHeight` when no
 * explicit width/height props are given, and — critically — never listens
 * for a `resize` event to correct itself afterwards (verified by reading
 * node_modules/force-graph/src/force-graph.js: the `width`/`height` Kapsule
 * props only recompute `onChange`, i.e. when the CONSUMER passes a new
 * value; there is no internal `window.addEventListener('resize', ...)`).
 * The canvas ends up permanently sized to whatever window.innerHeight was
 * at first mount — wrong and frozen in iOS-PWA standalone mode.
 *
 * Fix: never read window.innerWidth/innerHeight. Measure the actual
 * rendered container instead — it lives under the app-shell's already
 * -correct 100lvh height chain, so its `getBoundingClientRect()` is right
 * regardless of which viewport unit lies on this device. `ResizeObserver`
 * additionally keeps it correct across container resizes (tab switches,
 * split-view, orientation change) — force-graph itself can't do that
 * without an explicit width/height push.
 */
export function useContainerSize<T extends HTMLElement>(): {
  ref: RefObject<T | null>;
  size: ContainerSize | null;
} {
  const ref = useRef<T | null>(null);
  const [size, setSize] = useState<ContainerSize | null>(null);

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;

    // Seed synchronously (before ResizeObserver's first async callback) so
    // the very first paint already uses the real container size instead of
    // a 0x0 → correct-size flash, or worse, a fallback to window.* lies.
    const measure = () => {
      const rect = el.getBoundingClientRect();
      setSize({ width: Math.round(rect.width), height: Math.round(rect.height) });
    };
    measure();

    const ro = new ResizeObserver((entries) => {
      const entry = entries[0];
      if (!entry) return;
      const { width, height } = entry.contentRect;
      setSize({ width: Math.round(width), height: Math.round(height) });
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  return { ref, size };
}
