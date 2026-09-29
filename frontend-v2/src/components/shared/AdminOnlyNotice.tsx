"use client";

import { Lock } from "lucide-react";

/** Quiet placeholder where an admin-only control (terminal, shell, session
 *  input) would be. The text comes from the caller's i18n namespace. */
export function AdminOnlyNotice({ message, compact = false }: { message: string; compact?: boolean }) {
  return (
    <div
      role="note"
      data-testid="admin-only-notice"
      className={
        compact
          ? "flex items-center gap-2 px-3 py-2 text-xs"
          : "flex flex-col items-center justify-center flex-1 gap-3 text-xs bg-[var(--color-bg-surface)]"
      }
      style={{ color: "var(--color-text-muted)" }}
    >
      <Lock size={compact ? 14 : 28} style={{ opacity: compact ? 0.7 : 0.35 }} aria-hidden />
      <span>{message}</span>
    </div>
  );
}
