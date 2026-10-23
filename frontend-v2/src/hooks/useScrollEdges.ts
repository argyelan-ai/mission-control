import { useCallback, useEffect, useState } from "react";

/**
 * Whether a horizontally scrolling strip hides content at its start or end
 * (task tabs on a phone: 545 px of tabs in 390 px, round-2 Q5 / t4 N9). The
 * strip shows a scroll button only where something is actually hidden — a
 * static fade did not say that "Timeline" and "History" exist at all.
 * `ref` is a callback ref; `watch` re-measures when the content changes.
 */
export function useScrollEdges<T extends HTMLElement>(watch: unknown) {
  const [el, setEl] = useState<T | null>(null);
  const [edges, setEdges] = useState({ start: false, end: false });
  const ref = useCallback((node: T | null) => setEl(node), []);
  useEffect(() => {
    if (!el) {
      setEdges({ start: false, end: false });
      return;
    }
    const measure = () => {
      const start = el.scrollLeft > 1;
      const end = el.scrollLeft + el.clientWidth < el.scrollWidth - 1;
      setEdges((prev) => (prev.start === start && prev.end === end ? prev : { start, end }));
    };
    measure();
    el.addEventListener("scroll", measure, { passive: true });
    window.addEventListener("resize", measure);
    const ro = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(measure);
    ro?.observe(el);
    return () => {
      el.removeEventListener("scroll", measure);
      window.removeEventListener("resize", measure);
      ro?.disconnect();
    };
  }, [el, watch]);
  return { ref, el, ...edges };
}
