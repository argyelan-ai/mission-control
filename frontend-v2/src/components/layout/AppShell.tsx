"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useAppStore } from "@/lib/store";
import { api, getToken, getStoredUser, setStoredUser } from "@/lib/api";
import { AmbientBackground } from "./AmbientBackground";
import Sidebar from "./Sidebar";
import MobileNav, { MobileNavProvider, MobileTabBar } from "./MobileNav";
import CommandPalette from "@/components/shared/CommandPalette";
import ToastRenderer from "@/components/shared/ToastRenderer";
import { VoiceProvider, VoiceOverlay } from "@/components/voice/VoiceWidget";
import { useKeyboardShortcuts } from "@/hooks/useKeyboardShortcuts";

// Refresh the signed-in user from the server once per page load. The stored
// copy only exists after a form login and goes stale when the role or name
// changes; without it the shell showed "?" / "—" and a guessed role.
let userRefreshed = false;

export default function AppShell({
  children,
  fullHeight = false,
  mobileChromeless = false,
  mobileHideAppBar = false,
}: {
  children: React.ReactNode;
  fullHeight?: boolean;
  /** Auf dem Handy die App-Leiste UND die Tab-Leiste zurücktreten lassen,
   *  weil der Bildschirm seine eigene Kopfzeile mitbringt (Sessions-Chat).
   *
   *  Vorher stapelten sich dort drei Leisten übereinander plus eine
   *  88-px-Polsterung, die nur die App-Leiste freihalten sollte — zusammen
   *  rund 140 px, auf einem Handy über ein Sechstel des Bildes. Die
   *  Claude-App macht es mit EINER Leiste, und der Chat-Kopf kann genau das
   *  sein: er trägt Zurück-Pfeil, Namen und Optionen bereits.
   *
   *  Der Preis ist bewusst in Kauf genommen (Operator-Entscheid 19.08.2026):
   *  Aus dem Chat kommt man nur über den Zurück-Pfeil in einen anderen
   *  Bereich — genauso wie in jeder Handy-App mit einer geschobenen Ansicht.
   *  Auf dem Desktop ändert sich nichts: dort trägt keine der beiden Leisten
   *  überhaupt etwas bei (`md:hidden`). */
  mobileChromeless?: boolean;
  /** Auf dem Handy NUR die obere App-Leiste zurücktreten lassen, die
   *  Tab-Leiste bleibt (Task-Detail): die Kontextleiste des Bildschirms
   *  (‹ Aufgaben · ⋯) ersetzt dort die Wortmarke — DESIGN.md K12, eine
   *  mitlaufende Leiste kommt nie als dritte Leiste dazu. Nur mit `fullHeight`
   *  sinnvoll. Der Menü-Drawer bleibt erreichbar (Tab „Index"). */
  mobileHideAppBar?: boolean;
}) {
  const router = useRouter();
  const { setCurrentUser } = useAppStore();
  const [authorized, setAuthorized] = useState(false);

  useKeyboardShortcuts();

  // Auth guard
  useEffect(() => {
    const token = getToken();
    if (!token) {
      router.replace("/login");
      return;
    }

    const user = getStoredUser();
    if (user) {
      setCurrentUser(user);
    }

    setAuthorized(true);

    if (!userRefreshed) {
      userRefreshed = true;
      Promise.resolve()
        .then(() => api.auth.me())
        .then((fresh) => {
          const next = { id: fresh.id, email: fresh.email, name: fresh.name, role: fresh.role };
          setStoredUser(next);
          setCurrentUser(next);
        })
        .catch(() => {
          // Offline / expired token: keep the stored copy; the next request's
          // 401 handling takes care of a dead session.
          userRefreshed = false;
        });
    }
  }, [router, setCurrentUser]);

  if (!authorized) {
    return (
      <div
        className="min-h-dvh flex items-center justify-center"
        style={{ backgroundColor: "var(--color-p2-bg)" }}
      >
        <div
          className="w-5 h-5 rounded-full border-2 border-t-transparent animate-spin"
          style={{ borderColor: "var(--color-p2-amb)", borderTopColor: "transparent" }}
        />
      </div>
    );
  }

  return (
    <VoiceProvider>
    <MobileNavProvider>
    <div
      className="flex overflow-hidden relative app-shell-height"
      style={{ backgroundColor: "var(--color-p2-bg)" }}
    >
      <AmbientBackground />

      {/* Mobile navigation */}
      {!mobileChromeless && <MobileNav showBar={!mobileHideAppBar} />}

      {/* Desktop: one column carries board, search, navigation and status
          (Shell v4). The former WorkspaceSwitcher rail, TopBar and StatusBar
          folded into it — see components/layout/Sidebar.tsx. */}
      <div className="hidden md:flex h-full relative z-10 p-2 pr-0">
        <Sidebar />
      </div>

      {/* Main content area — deliberately NO z-index: a z-index here would
          open a stacking context and trap every page overlay (z-40/z-50)
          beneath the fixed mobile app bar (z-40). `relative` alone already
          paints it above the z-0 ambient background (later in tree order). */}
      <div className="flex flex-col flex-1 min-w-0 overflow-hidden relative">
        {fullHeight ? (
          // Full-height mode: no page scroll, but KEEP main-content-pt,
          // horizontal padding, AND the max-w-[1600px] mx-auto wrap so
          // child pages line up at the same left edge as the default
          // (scrolling) layout. The wrap also gives flex-col + flex-1 so
          // the graph canvas can partition the remaining vertical space.
          <main
            className={`flex-1 overflow-hidden flex flex-col main-content-pb px-4 md:px-6 lg:px-8 ${
              // Die Polsterung MUSS mitverschwinden: sie hält nur die fixe
              // App-Leiste frei. Bliebe sie stehen, wäre statt der Leiste ein
              // gleich grosses Loch da — die Seite wäre kürzer statt höher.
              // `md:pt-6` hält den Desktop-Abstand, den main-content-pt dort
              // beisteuert (1.5rem), unverändert. Ein "pt-0" davor braucht es
              // nicht — Tailwinds Preflight setzt padding ohnehin auf 0.
              mobileChromeless || mobileHideAppBar ? "md:pt-6" : "main-content-pt"
            }`}
          >
            <div className="mx-auto w-full max-w-[1600px] flex flex-col flex-1 min-h-0">
              {children}
            </div>
          </main>
        ) : (
          <main
            className="flex-1 overflow-y-auto overflow-x-hidden main-content-pt main-content-pb px-4 md:px-6 lg:px-8"
          >
            <div className="mx-auto w-full max-w-[1600px]">
              {children}
            </div>
          </main>
        )}
        {/* Mobile-Tab-Leiste: bewusst KEIN position:fixed. Als Flex-Kind der
            h-dvh-Box sitzt sie zuverlässig am unteren Rand — auch auf iOS,
            wo der Viewport-Bezug von `fixed` unzuverlässig ist. Auf Desktop
            ausgeblendet (md:hidden); dort trägt die Sidebar-Fusszeile den
            Systemzustand. */}
        {!mobileChromeless && <MobileTabBar />}
      </div>

      {/* Global Command Palette */}
      <CommandPalette />

      {/* Voice Assistant Overlay (Drawer + Error-Toast). Button selbst ist
          in MobileNav (mobile) und Sidebar (desktop) integriert. */}
      <VoiceOverlay />

      {/* Toast notifications (app-wide, driven by lib/notify.ts) */}
      <ToastRenderer />
    </div>
    </MobileNavProvider>
    </VoiceProvider>
  );
}
