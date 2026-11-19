"use client";

import { useCallback, useRef, useState } from "react";

/**
 * Live width of an element, through a CALLBACK ref.
 *
 * `useContainerSize` measures in a mount effect; an element that appears
 * later (the sessions page renders its island only after AppShell's auth
 * check) is never measured there — the side panel then had no width cap and
 * could be dragged to 2320 px over a 1038 px island (live finding,
 * 01.11.2026). A callback ref fires whenever the element itself attaches.
 */
export function useElementWidth<T extends HTMLElement>(): [(el: T | null) => void, number] {
  const [width, setWidth] = useState(0);
  const observer = useRef<ResizeObserver | null>(null);

  const ref = useCallback((el: T | null) => {
    observer.current?.disconnect();
    observer.current = null;
    if (!el) return;
    setWidth(Math.round(el.getBoundingClientRect().width));
    if (typeof ResizeObserver === "undefined") return;
    observer.current = new ResizeObserver((entries) => {
      const entry = entries[0];
      if (entry) setWidth(Math.round(entry.contentRect.width));
    });
    observer.current.observe(el);
  }, []);

  return [ref, width];
}
