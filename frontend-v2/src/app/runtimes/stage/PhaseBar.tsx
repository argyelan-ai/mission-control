"use client";

/**
 * PhaseBar — der Wechsel-Zustand ersetzt die Aktionen (Spec §2 Zone 4,
 * "Während Wechsel"). Wiederverwendet PhaseIndicator statt einer zweiten
 * evicting→launching→loading-Leiste.
 */

import { C } from "@/lib/colors";
import { PhaseIndicator } from "./PhaseIndicator";

export function PhaseBar({
  phase,
  targetLabel,
  onCancel,
  cancelLabel,
}: {
  phase?: string | null;
  targetLabel?: string | null;
  onCancel?: () => void;
  cancelLabel: string;
}) {
  return (
    <div className="flex items-center gap-3 px-4 py-3 flex-wrap" style={{ borderTop: `1px solid ${C.borderSubtle}` }}>
      <PhaseIndicator phase={phase} message={targetLabel ?? null} />
      {onCancel && (
        <button
          type="button"
          onClick={onCancel}
          className="text-xs px-3 py-2 rounded-md cursor-pointer shrink-0"
          style={{ border: `1px solid ${C.borderActive}`, color: C.textSecondary }}
        >
          {cancelLabel}
        </button>
      )}
    </div>
  );
}
