"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { useKeyboardInset } from "@/hooks/useKeyboardInset";
import { startThemeSync } from "@/lib/theme";

export function Providers({ children }: { children: React.ReactNode }) {
  // iOS: Tastaturhöhe als --keyboard-inset bereitstellen (MOBILE-SPEC M9)
  useKeyboardInset();
  // Theme (ADR-087): follow OS switches ("system") and other tabs.
  useEffect(() => startThemeSync(), []);
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
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
}
