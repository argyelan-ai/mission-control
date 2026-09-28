"use client";

import { usePathname } from "next/navigation";
import { useEffect } from "react";
import { sendPageView } from "@/lib/pageBeacon";

/** Counts one page view per route change (E0 page usage, lib/pageBeacon.ts). */
export function usePageBeacon(): void {
  const pathname = usePathname();
  useEffect(() => {
    if (pathname) sendPageView(pathname);
  }, [pathname]);
}
