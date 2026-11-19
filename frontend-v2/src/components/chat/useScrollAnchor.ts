"use client";

/**
 * Anchored toggles (spec A §6.3, M3): when the reader opens or closes
 * something in the chat, the row they tapped stays where it is on screen.
 *
 * The chat's ONE scroll owner (ChatView) provides `capture` / `restore`:
 * `capture(el)` before the state change ends following (the tapped row, not
 * the end, is now what the reader looks at) and remembers the row's place in
 * the viewport; `restore()` after the commit moves the scroll position by
 * however far the row moved. Without a provider both are no-ops, so a row
 * outside the chat behaves as before.
 */
import { createContext, useCallback, useContext, useLayoutEffect, useRef, useState } from "react";

export interface ScrollAnchor {
  capture(el: Element | null): void;
  restore(): void;
}

const NOOP: ScrollAnchor = { capture() {}, restore() {} };

export const ScrollAnchorContext = createContext<ScrollAnchor>(NOOP);

export function useScrollAnchor(): ScrollAnchor {
  return useContext(ScrollAnchorContext);
}

/**
 * Runs a state change anchored: `run(fn)` captures the row behind `ref`,
 * applies `fn`, and restores after that commit. For rows whose open state is
 * derived (an automatic default the reader can override).
 */
export function useAnchoredAction(anchored = true) {
  const anchor = useScrollAnchor();
  const ref = useRef<HTMLElement | null>(null);
  const pending = useRef(false);
  const [tick, setTick] = useState(0);
  const run = useCallback(
    (fn: () => void) => {
      if (anchored) {
        anchor.capture(ref.current);
        pending.current = true;
        setTick((n) => n + 1);
      }
      fn();
    },
    [anchor, anchored],
  );
  useLayoutEffect(() => {
    if (!pending.current) return;
    pending.current = false;
    anchor.restore();
  }, [tick, anchor]);
  return { ref, run };
}

/**
 * An open/closed state whose user toggles are anchored. `ref` goes on the
 * tapped row. `anchored = false` keeps the plain behaviour (a row in an
 * ordinary chat turn, where nothing changed).
 */
export function useAnchoredToggle(initial: boolean, anchored = true) {
  const [open, setOpen] = useState(initial);
  const { ref, run } = useAnchoredAction(anchored);
  const toggle = useCallback(() => run(() => setOpen((v) => !v)), [run]);
  return { open, setOpen, toggle, ref };
}
