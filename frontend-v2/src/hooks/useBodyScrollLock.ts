"use client";

import { useEffect } from "react";

/**
 * Body-Scroll-Lock für Modals/Drawer/Sheets — iOS-fest.
 *
 * `overflow: hidden` auf <body> stoppt Touch-Scrolling auf iOS Safari NICHT
 * (Hintergrund scrollt weiter, Scroll-Position springt). Einzig zuverlässig
 * ist die Fixed-Position-Technik: body fixieren, Scroll-Offset merken,
 * beim Unlock exakt zurückspringen. (MOBILE-SPEC M4)
 *
 * Verwendung: `useBodyScrollLock(open)` in jeder Modal-/Sheet-Komponente.
 * Mehrere gleichzeitige Locks werden über einen Zähler entschachtelt.
 */
let lockCount = 0;
let savedScrollY = 0;

function lock() {
  if (lockCount === 0) {
    savedScrollY = window.scrollY;
    const body = document.body;
    body.style.position = "fixed";
    body.style.top = `-${savedScrollY}px`;
    body.style.left = "0";
    body.style.right = "0";
    body.style.width = "100%";
    body.style.overflow = "hidden";
  }
  lockCount++;
}

function unlock() {
  lockCount = Math.max(0, lockCount - 1);
  if (lockCount === 0) {
    const body = document.body;
    body.style.position = "";
    body.style.top = "";
    body.style.left = "";
    body.style.right = "";
    body.style.width = "";
    body.style.overflow = "";
    window.scrollTo(0, savedScrollY);
  }
}

export function useBodyScrollLock(active: boolean) {
  useEffect(() => {
    if (!active) return;
    lock();
    return unlock;
  }, [active]);
}
