import { useCallback, useEffect, useState } from "react";

/**
 * True while the element's content is taller than the element (a clamped
 * preview that actually cuts something). Lets a fade mask sit only on text
 * that continues — a short text must not lose its last line to the fade.
 * `ref` is a callback ref, so an element that mounts later is measured too;
 * `watch` re-measures when the content changes.
 */
export function useOverflows<T extends HTMLElement>(watch: unknown) {
  const [el, setEl] = useState<T | null>(null);
  const [overflows, setOverflows] = useState(false);
  const ref = useCallback((node: T | null) => setEl(node), []);
  useEffect(() => {
    if (!el) {
      setOverflows(false);
      return;
    }
    const measure = () => setOverflows(el.scrollHeight > el.clientHeight + 1);
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [el, watch]);
  return { ref, overflows };
}
