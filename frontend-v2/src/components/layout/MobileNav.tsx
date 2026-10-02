"use client";

import { createContext, useContext, useEffect, useMemo, useState } from "react";
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
   Geteilter Zustand der Handy-Navigation.

   Das ⊕-Blatt (components/layout/QuickSheet.tsx) ist das EINE Handy-Menü:
   alle Bereiche, Board, Konto. Es hängt an der unteren Leiste
   (<MobileTabBar />), lebt aber ausserhalb davon — die Leiste ist ein
   normales Flex-Kind der App-Shell (kein `fixed`, damit sie auf iOS wirklich
   am unteren Rand der h-dvh-Box klebt). Den früheren Index-Drawer gibt es
   nicht mehr (Operator 02.10.2026: „alle Menüpunkte ins ⊕-Menü").
   ──────────────────────────────────────────────────────────────── */
type MobileNavState = {
  /** The ⊕ "New" sheet (components/layout/QuickSheet.tsx). */
  quickOpen: boolean;
  setQuickOpen: (v: boolean) => void;
};

const MobileNavContext = createContext<MobileNavState | null>(null);

export function MobileNavProvider({ children }: { children: React.ReactNode }) {
  const [quickOpen, setQuickOpen] = useState(false);
  const pathname = usePathname();

  // Close on route change
  useEffect(() => {
    setQuickOpen(false);
  }, [pathname]);

  // Prevent body scroll while the sheet is up — iOS-fest via Fixed-Position-Technik (MOBILE-SPEC M4)
  useBodyScrollLock(quickOpen);

  // Esc closes the sheet like every other overlay (panel register rule 4).
  useEscapeKey(() => setQuickOpen(false), quickOpen);

  const value = useMemo(() => ({ quickOpen, setQuickOpen }), [quickOpen]);

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
 * MobileNav — the phone's top app bar: wordmark (home link) left, voice
 * right. Navigation itself lives in the tab bar and its ⊕ sheet.
 */
export default function MobileNav({ showBar = true }: {
  /** false = no bar: a screen with its own top bar (task detail on the phone). */
  showBar?: boolean;
} = {}) {
  if (!showBar) return null;
  return (
    // pt-island hält Inhalt unter der Dynamic Island; opak statt backdrop-blur (kein iOS Jank).
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
  );
}
