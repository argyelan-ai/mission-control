"use client";

import { useEffect, useState } from "react";

/**
 * True while the on-screen keyboard is up (phone). Same measurement as
 * `useKeyboardInset` (VisualViewport: layout height minus visual height,
 * wobbles under 80 px from the URL bar ignored) — but as React state, so
 * bottom bars can step aside: with the keyboard up the input belongs right
 * above it, and a tab or action bar there would only eat the little room left.
 */
export function useKeyboardOpen(): boolean {
  const [open, setOpen] = useState(false);
  useEffect(() => {
    const vv = typeof window !== "undefined" ? window.visualViewport : null;
    if (!vv) return;
    const update = () => setOpen(window.innerHeight - vv.height - vv.offsetTop > 80);
    update();
    vv.addEventListener("resize", update);
    vv.addEventListener("scroll", update);
    return () => {
      vv.removeEventListener("resize", update);
      vv.removeEventListener("scroll", update);
    };
  }, []);
  return open;
}
