"use client";

/**
 * PhaseIndicator — the evicting → launching → loading strip shown while a
 * recipe switch / eviction / cold start is in progress.
 *
 * Extracted from the now-removed SlotStage.tsx (PR 6 "Schliff", v1 retired)
 * into its own file under `stage/` — it was already exported there purely so
 * `PhaseBar.tsx` could reuse it; now that SlotStage itself is gone, this is
 * its real home. Same component, same behaviour, no visual change.
 */

import { C } from "@/lib/colors";
import type { RuntimeLiveStatus } from "@/lib/types";

const PHASES: Array<NonNullable<RuntimeLiveStatus["phase"]>> = ["evicting", "launching", "loading"];

export function PhaseIndicator({ phase, message }: { phase?: string | null; message?: string | null }) {
  return (
    <div className="flex-1 flex items-center gap-2" data-testid="phase-indicator">
      <div className="flex items-center gap-1.5 text-xs font-mono">
        {PHASES.map((p, i) => (
          <span key={p} className="flex items-center gap-1.5">
            <span style={{ color: p === phase ? C.accent : C.textDim, fontWeight: p === phase ? 600 : 400 }}>
              {p}
            </span>
            {i < PHASES.length - 1 && <span style={{ color: C.textDim }}>→</span>}
          </span>
        ))}
      </div>
      {message && <span className="text-xs truncate" style={{ color: C.textMuted }}>{message}</span>}
    </div>
  );
}
