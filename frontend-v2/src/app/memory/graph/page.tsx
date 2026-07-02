"use client";

/**
 * Legacy /memory/graph route — redirects to /memory?view=graph.
 * The graph is now a tab on the main MemoryPage (consolidated 2026-05-15).
 */

import { useEffect } from "react";
import { useRouter } from "next/navigation";

export default function MemoryGraphRedirectPage() {
  const router = useRouter();
  useEffect(() => {
    router.replace("/memory?view=graph");
  }, [router]);
  return null;
}
