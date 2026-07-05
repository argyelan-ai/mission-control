"use client";

/**
 * Canonical terminal size + scale-to-fit (Sessions / Agent-CLI viewers).
 *
 * Every browser viewer used to FitAddon-resize the shared tmux window
 * (TIOCSWINSZ): a phone viewer squeezed the agent's TUI to ~40 columns,
 * multiple viewers made the size flap, and the agent's rendering ended up
 * distorted for everyone.
 *
 * Instead, all viewers attach with ONE canonical size — the tmux window
 * stays stable no matter who watches — and the rendered terminal is scaled
 * down with a CSS transform to fit the viewer's container ("fit"), with an
 * optional 1:1 mode that pans/scrolls instead.
 */

import { useEffect, useState, type RefObject } from "react";
import type { Terminal } from "@xterm/xterm";

export const TERM_FONT_FAMILY =
  '"JetBrains Mono", "Symbols Nerd Font Mono", "Fira Code", monospace';

// xterm brightens too-dark foregrounds against the bg automatically —
// same mechanism as VS Code's terminal (default there is 4.5 as well).
// Fixes barely-readable dim/gray TUI text without changing the background.
export const TERM_MIN_CONTRAST = 4.5;

export const TERM_COLS = 168;
export const TERM_ROWS = 45;

export type TermViewMode = "fit" | "native";

export function useTerminalScale(
  outerRef: RefObject<HTMLElement | null>,
  term: Terminal | null,
  mode: TermViewMode,
) {
  const [scale, setScale] = useState(1);
  const [size, setSize] = useState<{ w: number; h: number } | null>(null);

  useEffect(() => {
    if (!term || !outerRef.current) return;
    const outer = outerRef.current;

    const measure = () => {
      // .xterm-screen is sized to cols×cellW / rows×cellH — the terminal's
      // natural pixel size, independent of the mount container.
      const screen = term.element?.querySelector(".xterm-screen") as HTMLElement | null;
      if (!screen || !outer) return;
      const w = screen.offsetWidth || 1;
      const h = screen.offsetHeight || 1;
      setSize({ w, h });
      setScale(mode === "fit" ? Math.min(1, outer.clientWidth / w) : 1);
    };

    // Double-RAF: xterm needs a frame to lay out after resize(cols, rows).
    const raf = requestAnimationFrame(() => requestAnimationFrame(measure));
    const ro = new ResizeObserver(measure);
    ro.observe(outer);
    // Cell metrics change when the mono font finishes loading — the screen
    // element resizes then, so observing it keeps the scale honest.
    const screen = term.element?.querySelector(".xterm-screen");
    if (screen) ro.observe(screen);
    return () => {
      cancelAnimationFrame(raf);
      ro.disconnect();
    };
  }, [term, mode, outerRef]);

  return { scale, size };
}
