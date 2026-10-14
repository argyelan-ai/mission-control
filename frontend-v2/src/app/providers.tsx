"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { useKeyboardInset } from "@/hooks/useKeyboardInset";
import { usePageBeacon } from "@/hooks/usePageBeacon";
import { startThemeSync } from "@/lib/theme";
import { VoiceCallRoot } from "@/components/voice/live/VoiceLiveCall";

export function Providers({ children }: { children: React.ReactNode }) {
  // iOS: Tastaturhöhe als --keyboard-inset bereitstellen (MOBILE-SPEC M9)
  useKeyboardInset();
  // Theme (ADR-087): follow OS switches ("system") and other tabs.
  useEffect(() => startThemeSync(), []);
  // E0 page usage: one counter per route pattern and day, no user data.
  usePageBeacon();
  const [queryClient] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            staleTime: 5_000,          // 5s — Daten sind schnell "stale"
            gcTime: 5 * 60_000,        // 5min Cache bevor Garbage Collection
            refetchOnWindowFocus: true, // Sofort refreshen bei Tab-Wechsel
            retry: 1,
          },
        },
      })
  );

  return (
    <QueryClientProvider client={queryClient}>
      {/* The live voice call lives HERE, above every page: each page renders
          its own AppShell, so anything inside AppShell remounts on a route
          change (voice PR 5 live finding). */}
      <VoiceCallRoot>{children}</VoiceCallRoot>
    </QueryClientProvider>
  );
}
