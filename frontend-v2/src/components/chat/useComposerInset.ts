"use client";

import { useEffect, useState } from "react";

/** Gap kept between the overlaid panel and the composer pill. */
const GAP = 8;

/**
 * How far an overlaid side panel must stop above the island's bottom so the
 * composer (input, model chip, send button) stays free and usable — real
 * browser finding at 1024 px: the panel lay over the send button.
 *
 * Measured, not guessed: the composer grows with its text and its chips, so
 * the inset follows a ResizeObserver on the island and the composer pill.
 * Returns 0 while `active` is false or no composer is on screen (head chats,
 * groups) — the panel then runs to the bottom as before.
 */
export function useComposerInset(active: boolean, refreshKey?: unknown): number {
  const [inset, setInset] = useState(0);
  useEffect(() => {
    if (!active) {
      setInset(0);
      return;
    }
    const island = document.querySelector<HTMLElement>('[data-testid="chat-island"]');
    if (!island) return;
    const measure = () => {
      const pill = island.querySelector<HTMLElement>('[data-testid="composer-dropzone"]');
      if (!pill) {
        setInset(0);
        return;
      }
      const bottom = island.getBoundingClientRect().bottom;
      const top = pill.getBoundingClientRect().top;
      setInset(Math.max(0, Math.round(bottom - top + GAP)));
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(measure);
    ro.observe(island);
    const pill = island.querySelector<HTMLElement>('[data-testid="composer-dropzone"]');
    if (pill) ro.observe(pill);
    return () => ro.disconnect();
  }, [active, refreshKey]);
  return inset;
}
