"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { usePathname } from "next/navigation";
import Link from "next/link";
import { VoiceButton } from "@/components/voice/VoiceWidget";
import { P2 } from "@/lib/colors";
import { useBodyScrollLock } from "@/hooks/useBodyScrollLock";
import { useEscapeKey } from "@/hooks/useEscapeKey";

// Wordmark (gleiche Logik wie Sidebar — env-getrieben)
const _BRAND = process.env.NEXT_PUBLIC_BRAND || "Mission.Control";
const _dot = _BRAND.lastIndexOf(".");
const BRAND_MAIN = _dot > 0 ? _BRAND.slice(0, _dot) : _BRAND;
const BRAND_ACCENT = _dot > 0 ? _BRAND.slice(_dot) : "";

/* ────────────────────────────────────────────────────────────────
   Geteilter Zustand des Handy-Menüs.

   Das ⊕-Blatt (QuickSheet.tsx, Variante B „Zwei Ebenen") ist das einzige
   Handy-Menü. Es hat zwei Ebenen: „root" (Starten: Neuer Job, Sprache,
   letzter Chat, Seiten ›, Einstellungen ›) und „pages" (alle Seiten, die
   nicht in der Leiste stehen). Die alte Schublade („Mehr…") gibt es nicht
   mehr; Board, Konto und Abmelden stehen am Handy in den Einstellungen.
   ──────────────────────────────────────────────────────────────── */
export type QuickLevel = "root" | "pages";

type MobileNavState = {
  /** The ⊕ menu sheet (components/layout/QuickSheet.tsx). */
  quickOpen: boolean;
  setQuickOpen: (v: boolean) => void;
  /** Which level of the sheet shows. Every open starts at "root". */
  quickLevel: QuickLevel;
  setQuickLevel: (v: QuickLevel) => void;
};

const MobileNavContext = createContext<MobileNavState | null>(null);

export function MobileNavProvider({ children }: { children: React.ReactNode }) {
  const [quickOpen, setQuickOpenRaw] = useState(false);
  const [quickLevel, setQuickLevel] = useState<QuickLevel>("root");
  const pathname = usePathname();

  // Opening always lands on level 1; closing forgets level 2.
  const setQuickOpen = useCallback((v: boolean) => {
    setQuickOpenRaw(v);
    if (!v) setQuickLevel("root");
  }, []);

  // Close on route change
  useEffect(() => {
    setQuickOpenRaw(false);
    setQuickLevel("root");
  }, [pathname]);

  // Prevent body scroll while the sheet is up — iOS-fest via Fixed-Position-Technik (MOBILE-SPEC M4)
  useBodyScrollLock(quickOpen);

  // Esc = one step back: level 2 → level 1, level 1 → closed (panel register rule 4).
  useEscapeKey(() => {
    if (quickLevel === "pages") setQuickLevel("root");
    else setQuickOpen(false);
  }, quickOpen);

  const value = useMemo(
    () => ({ quickOpen, setQuickOpen, quickLevel, setQuickLevel }),
    [quickOpen, setQuickOpen, quickLevel],
  );

  return <MobileNavContext.Provider value={value}>{children}</MobileNavContext.Provider>;
}

export function useMobileNav(): MobileNavState {
  const ctx = useContext(MobileNavContext);
  if (!ctx) {
    throw new Error("useMobileNav must be used inside <MobileNavProvider>");
  }
  return ctx;
}

/**
 * MobileNav — the phone's top app bar: wordmark (→ Home) left, voice right.
 * Stays `fixed` (an overlay-like bar above the scrolling content). The menu
 * itself is the ⊕ sheet; this file only holds the bar and the shared state.
 */
export default function MobileNav({ showBar = true }: {
  /** false = a screen with its own top bar (task detail on the phone) hides
   *  the wordmark bar. */
  showBar?: boolean;
} = {}) {
  if (!showBar) return null;
  return (
    <>
      {/* Top bar — Wordmark links (Home-Link), Voice rechts. pt-island hält
          Inhalt unter der Dynamic Island; opak statt backdrop-blur (kein iOS Jank). */}
      <header
        className="fixed top-0 left-0 right-0 z-40 flex items-end justify-between px-4 md:hidden pt-island"
        style={{
          paddingBottom: "0.5rem",
          minHeight: "calc(env(safe-area-inset-top) + 3.5rem)",
          backgroundColor: "var(--color-p2-pan)",
          borderBottom: "1px solid var(--color-p2-line2)",
        }}
      >
        <Link
          href="/"
          className="flex items-center h-11 cursor-pointer"
          aria-label="Home"
          style={{
            color: "var(--color-p2-txt)",
            fontFamily: "var(--font-p2-display)",
            fontWeight: 700,
            fontSize: "15px",
            letterSpacing: "0.02em",
          }}
        >
          {BRAND_MAIN}
          <span style={{ color: P2.amb }}>{BRAND_ACCENT}</span>
        </Link>

        <div className="flex items-center gap-2">
          <VoiceButton size={40} variant="header" />
        </div>
      </header>
    </>
  );
}
