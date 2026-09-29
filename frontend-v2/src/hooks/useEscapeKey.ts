"use client";

import { useEffect, useLayoutEffect, useRef } from "react";

/**
 * Call `onEscape` when Esc is pressed (window keydown) — the shared way for a
 * dialog, sheet or panel to close on Esc (panel register rule 4).
 *
 * The listener is bound ONCE per `enabled` phase and always calls the latest
 * `onEscape` through a ref. That matters because callers usually pass an
 * inline `onClose={() => setOpen(false)}`, i.e. a new function per render.
 * With `[onClose]` as effect dependency, every re-render removed and re-added
 * the window listener. On a REAL key press the browser runs a microtask
 * checkpoint after each listener, so React can re-render between two window
 * keydown listeners — the dialog's listener was then removed before its turn
 * and the Esc press got lost (DOM rule: a listener removed during dispatch is
 * skipped, a newly added one is not called). A synthetic event dispatched
 * from script has no such checkpoint, which is why it still worked in tests.
 */
export function useEscapeKey(onEscape: (e: KeyboardEvent) => void, enabled = true) {
  const handlerRef = useRef(onEscape);
  useLayoutEffect(() => {
    handlerRef.current = onEscape;
  });

  useEffect(() => {
    if (!enabled) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") handlerRef.current(e);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [enabled]);
}
