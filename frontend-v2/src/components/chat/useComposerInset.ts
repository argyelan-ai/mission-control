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
 * the inset follows a ResizeObserver on the island and the composer pill, and
 * a MutationObserver on the island for a pill that mounts late or is replaced.
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
    const ro = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(() => measure());
    // The pill being watched. It can mount after the panel opened (chat
    // still loading) or be replaced by a new node, so every measure looks it
    // up again and moves the ResizeObserver onto the current one.
    let watched: HTMLElement | null = null;
    const measure = () => {
      const pill = findPill();
      if (pill !== watched && ro) {
        if (watched) ro.unobserve(watched);
        if (pill) ro.observe(pill);
      }
      watched = pill;
      if (!pill) {
        setInset(0);
        return;
      }
      const bottom = island.getBoundingClientRect().bottom;
      const top = pill.getBoundingClientRect().top;
      setInset(Math.max(0, Math.round(bottom - top + GAP)));
    };
    ro?.observe(island);
    // A pill added or swapped inside the island does not resize the island,
    // so a MutationObserver catches it. Streamed messages mutate the island
    // all the time: only a changed pill node triggers a (layout-reading)
    // measure; size changes of the same pill stay with the ResizeObserver.
    const findPill = () => island.querySelector<HTMLElement>('[data-testid="composer-dropzone"]');
    const mo =
      typeof MutationObserver === "undefined"
        ? null
        : new MutationObserver(() => {
            if (findPill() !== watched) measure();
          });
    mo?.observe(island, { childList: true, subtree: true });
    measure();
    return () => {
      ro?.disconnect();
      mo?.disconnect();
    };
  }, [active, refreshKey]);
  return inset;
}
