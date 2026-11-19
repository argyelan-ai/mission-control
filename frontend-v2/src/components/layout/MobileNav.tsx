"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { usePathname } from "next/navigation";
import Link from "next/link";
import { NavVoiceButton } from "@/components/voice/live/NavVoiceButton";
import { DrawerButton } from "./ChatDrawer";
import { useTranslations } from "next-intl";
import { C, P2 } from "@/lib/colors";
import { useBodyScrollLock } from "@/hooks/useBodyScrollLock";
import { useEscapeKey } from "@/hooks/useEscapeKey";

// Wordmark (gleiche Logik wie Sidebar — env-getrieben)
const _BRAND = process.env.NEXT_PUBLIC_BRAND || "Mission.Control";
const _dot = _BRAND.lastIndexOf(".");
const BRAND_MAIN = _dot > 0 ? _BRAND.slice(0, _dot) : _BRAND;
const BRAND_ACCENT = _dot > 0 ? _BRAND.slice(_dot) : "";

/* ────────────────────────────────────────────────────────────────
   Geteilter Zustand der Handy-Navigation.

   Seit 18.11.2026 (Operator-Entscheid, Variante C + Menü aus A) ist die
   Chat-Schublade (ChatDrawer.tsx) das Handy-Menü: Chats, darunter Modelle ·
   Wissen · Einstellungen. Das ⊕-Blatt (QuickSheet.tsx) hängt die Shell nicht
   mehr ein; sein Zustand bleibt hier, bis es mit Beleg entfernt wird.
   ──────────────────────────────────────────────────────────────── */
export type QuickLevel = "root" | "pages";

type MobileNavState = {
  /** The ⊕ menu sheet (components/layout/QuickSheet.tsx). */
  quickOpen: boolean;
  setQuickOpen: (v: boolean) => void;
  /** Which level of the sheet shows. Every open starts at "root". */
  quickLevel: QuickLevel;
  setQuickLevel: (v: QuickLevel) => void;
  /** The chat drawer (components/layout/ChatDrawer.tsx), opened by the
   *  sidebar icon in the chat header and in the app bar. */
  drawerOpen: boolean;
  setDrawerOpen: (v: boolean) => void;
};

const MobileNavContext = createContext<MobileNavState | null>(null);

export function MobileNavProvider({ children }: { children: React.ReactNode }) {
  const [quickOpen, setQuickOpenRaw] = useState(false);
  const [quickLevel, setQuickLevel] = useState<QuickLevel>("root");
  const [drawerOpen, setDrawerOpen] = useState(false);
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
    setDrawerOpen(false);
  }, [pathname]);

  // Prevent body scroll while the sheet is up — iOS-fest via Fixed-Position-Technik (MOBILE-SPEC M4)
  // The drawer exists only below md. Widening the window with it open would
  // leave an invisible drawer holding the scroll lock (shell review A4).
  useEffect(() => {
    if (!drawerOpen || typeof window === "undefined" || !window.matchMedia) return;
    const mq = window.matchMedia("(min-width: 768px)");
    const onChange = () => {
      if (mq.matches) setDrawerOpen(false);
    };
    onChange();
    mq.addEventListener?.("change", onChange);
    return () => mq.removeEventListener?.("change", onChange);
  }, [drawerOpen]);

  useBodyScrollLock(quickOpen || drawerOpen);
  useEscapeKey(() => setDrawerOpen(false), drawerOpen);

  // Esc = one step back: level 2 → level 1, level 1 → closed (panel register rule 4).
  useEscapeKey(() => {
    if (quickLevel === "pages") setQuickLevel("root");
    else setQuickOpen(false);
  }, quickOpen);

  const value = useMemo(
    () => ({ quickOpen, setQuickOpen, quickLevel, setQuickLevel, drawerOpen, setDrawerOpen }),
    [quickOpen, setQuickOpen, quickLevel, drawerOpen],
  );

  return <MobileNavContext.Provider value={value}>{children}</MobileNavContext.Provider>;
}

/** Same state, or null outside the shell (a component rendered on its own,
 *  e.g. in a test) — for parts that simply step aside there. */
export function useMobileNavOptional(): MobileNavState | null {
  return useContext(MobileNavContext);
}

export function useMobileNav(): MobileNavState {
  const ctx = useContext(MobileNavContext);
  if (!ctx) {
    throw new Error("useMobileNav must be used inside <MobileNavProvider>");
  }
  return ctx;
}

/**
 * MobileNav — the phone's top app bar on every page but the chat. Left: the
 * chat drawer's sidebar icon, then either the page title (pages of the three
 * tabs — the bar then reads like the chat header: icon · title · action) or
 * the wordmark (→ the last chat). Right: the page's action, else voice.
 * Stays `fixed` (an overlay-like bar above the scrolling content).
 */
export default function MobileNav({
  showBar = true,
  title,
  action,
}: {
  /** false = a screen with its own top bar (task detail on the phone) hides
   *  the wordmark bar. */
  showBar?: boolean;
  /** Page title in the bar, styled like the chat header's title. */
  title?: string;
  /** The page's one action in the bar (K4: at most one next to the icon). */
  action?: React.ReactNode;
} = {}) {
  const t = useTranslations("cockpit");
  if (!showBar) return null;
  return (
    <>
      {/* pt-island hält Inhalt unter der Dynamic Island; opak statt
          backdrop-blur (kein iOS Jank). */}
      <header
        data-testid="mobile-app-bar"
        className="fixed top-0 left-0 right-0 z-40 flex items-end justify-between px-4 md:hidden pt-island"
        style={{
          paddingBottom: "0.5rem",
          minHeight: "calc(env(safe-area-inset-top) + 3.5rem)",
          backgroundColor: "var(--color-p2-pan)",
          borderBottom: "1px solid var(--color-p2-line2)",
        }}
      >
        <div className="flex items-center -ml-3 min-w-0">
          <DrawerButton />
          {title ? (
            <h1
              data-testid="mobile-app-bar-title"
              className="text-base leading-tight font-semibold truncate"
              style={{ color: C.textPrimary }}
            >
              {title}
            </h1>
          ) : (
            <Link
              href="/"
              className="flex items-center h-11 cursor-pointer"
              aria-label={t("openLastChat")}
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
          )}
        </div>

        <div className="flex items-center gap-2 shrink-0">
          {/* The page's action, else the lead agent's live voice (voice PR 5;
              Jarvis when that is off). */}
          {action ?? <NavVoiceButton size={40} variant="header" />}
        </div>
      </header>
    </>
  );
}
