"use client";

/**
 * /memory route — M.3 T8.
 * Now points to VaultMemoryPage (vault-backed, Editorial Codex aesthetic).
 * Legacy board_memory page preserved in LegacyMemoryPage.tsx (M.5 will delete it).
 */

import { Suspense } from "react";
import VaultMemoryPage from "@/components/vault/VaultMemoryPage";

export default function MemoryRoutePage() {
  return (
    <Suspense fallback={null}>
      <VaultMemoryPage />
    </Suspense>
  );
}
