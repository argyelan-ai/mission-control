"use client";

import { useEffect } from "react";

/**
 * Virtuelle-Tastatur-Höhe als CSS-Variable `--keyboard-inset` (px).
 *
 * iOS Safari unterstützt die VirtualKeyboard-API NICHT (WebKit #230225,
 * offen seit 2021). Einzig verlässlicher Weg: VisualViewport-API —
 * Differenz zwischen Layout- und Visual-Viewport = Tastaturhöhe.
 * (MOBILE-SPEC M9)
 *
 * Einmal app-weit mounten (providers.tsx). Konsumenten:
 *   padding-bottom: var(--keyboard-inset, 0px)
 * an fixierten Footern/Sheets, damit Buttons über der Tastatur bleiben.
 */
export function useKeyboardInset() {
  useEffect(() => {
    const vv = window.visualViewport;
    if (!vv) return;
    const root = document.documentElement;
    const update = () => {
      const inset = Math.max(
        0,
        window.innerHeight - vv.height - vv.offsetTop
      );
      // Nur „echte" Tastatur-Höhen setzen (URL-Bar-Wackler < 80px ignorieren)
      root.style.setProperty("--keyboard-inset", `${inset > 80 ? inset : 0}px`);
    };
    update();
    vv.addEventListener("resize", update);
    vv.addEventListener("scroll", update);
    return () => {
      vv.removeEventListener("resize", update);
      vv.removeEventListener("scroll", update);
      root.style.removeProperty("--keyboard-inset");
    };
  }, []);
}
