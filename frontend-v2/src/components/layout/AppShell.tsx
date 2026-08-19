"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useAppStore } from "@/lib/store";
import { getToken, getStoredUser } from "@/lib/api";
import { AmbientBackground } from "./AmbientBackground";
import Sidebar from "./Sidebar";
import WorkspaceSwitcher from "./WorkspaceSwitcher";
import MobileNav, { MobileNavProvider, MobileTabBar } from "./MobileNav";
import StatusBar from "./StatusBar";
import TopBar from "./TopBar";
import CommandPalette from "@/components/shared/CommandPalette";
import ToastRenderer from "@/components/shared/ToastRenderer";
import { VoiceProvider, VoiceOverlay } from "@/components/voice/VoiceWidget";
import { useKeyboardShortcuts } from "@/hooks/useKeyboardShortcuts";

export default function AppShell({
  children,
  fullHeight = false,
  mobileChromeless = false,
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
      {!mobileChromeless && <MobileNav />}

      {/* Desktop: WorkspaceSwitcher + Sidebar */}
      <div className="hidden md:flex h-full relative z-10">
        <WorkspaceSwitcher />
        <Sidebar />
      </div>

      {/* Main content area */}
      <div className="flex flex-col flex-1 min-w-0 overflow-hidden relative z-10">
        {/* P2 desktop chrome: channel strip above the content column */}
        <TopBar />
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
              // beisteuert (1.5rem), unverändert.
              mobileChromeless ? "pt-0 md:pt-6" : "main-content-pt"
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
        <StatusBar />
        {/* Mobile-Tab-Leiste: bewusst KEIN position:fixed. Als Flex-Kind der
            h-dvh-Box sitzt sie zuverlässig am unteren Rand — auch auf iOS,
            wo der Viewport-Bezug von `fixed` unzuverlässig ist. StatusBar ist
            auf Mobil ausgeblendet (hidden md:flex), die Tab-Leiste auf Desktop
            (md:hidden) — sie schliessen sich also gegenseitig aus. */}
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
