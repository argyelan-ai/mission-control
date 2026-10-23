"use client";

/**
 * "Not found" in the app frame: one sentence, one hint, one way back.
 *
 * The same quiet block for every record that is gone or never existed —
 * an agent (round-3 t4 S1), a scheduled job, and a URL that matches no page
 * at all (`app/not-found.tsx`, t4 S9). Before, an agent link spun forever
 * and a typo URL showed the framework's English page without any menu.
 */
import Link from "next/link";
import { ArrowLeft } from "lucide-react";
import { C } from "@/lib/colors";

export function NotFoundState({
  title,
  hint,
  backHref,
  backLabel,
}: {
  title: string;
  /** Why it may be gone. Left out when the cause is not "gone" (a failed load). */
  hint?: string | null;
  backHref: string;
  backLabel: string;
}) {
  return (
    <div
      data-testid="not-found-state"
      data-region="not-found"
      className="flex flex-col items-center justify-center h-full gap-3 py-16 px-4 text-center"
    >
      <p className="text-sm font-medium" style={{ color: C.textPrimary }}>
        {title}
      </p>
      {hint && (
        <p className="text-xs" style={{ color: C.textMuted }}>
          {hint}
        </p>
      )}
      <Link
        href={backHref}
        className="flex items-center gap-1.5 text-xs px-3 py-1.5 min-h-11 sm:min-h-0 rounded-md transition-colors"
        style={{ color: C.accent, border: `1px solid ${C.borderAccent}` }}
      >
        <ArrowLeft size={13} aria-hidden />
        {backLabel}
      </Link>
    </div>
  );
}
