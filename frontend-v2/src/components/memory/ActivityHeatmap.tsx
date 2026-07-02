/**
 * ActivityHeatmap — T8 control affordance (M.4)
 *
 * This component is a TOGGLE BUTTON, not a renderer.
 * The actual halo rendering happens inside MemoryGraph2D when showHeatmap=true
 * (see the nodeCanvasObject callback in MemoryGraph2D.tsx).
 *
 * Halos are radial glow discs drawn around each node, with radius and
 * opacity proportional to viewCount (logarithmic scale). Cold nodes (viewCount=0)
 * get no halo. Hot nodes get a translucent disc 1.6×log2(viewCount+1) units
 * wider than the core disc.
 *
 * Mount alongside the graph:
 *   const [showHeatmap, setShowHeatmap] = useState(false);
 *   <ActivityHeatmap enabled={showHeatmap} onToggle={setShowHeatmap} />
 *   <MemoryGraph2D showHeatmap={showHeatmap} ... />
 */

"use client";

import { C } from "@/lib/colors";

export interface ActivityHeatmapProps {
  /** Whether heatmap halos are currently visible */
  enabled: boolean;
  /** Called with the new state when the toggle is clicked */
  onToggle: (enabled: boolean) => void;
  className?: string;
}

export function ActivityHeatmap({ enabled, onToggle, className }: ActivityHeatmapProps) {
  return (
    <button
      type="button"
      onClick={() => onToggle(!enabled)}
      title={
        enabled
          ? "Heatmap deaktivieren — node-halos ausblenden"
          : "Heatmap aktivieren — viewCount als Leuchtring"
      }
      className={className}
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: "6px",
        padding: "4px 10px",
        borderRadius: "4px",
        fontSize: "11px",
        fontFamily: "'Geist Mono', monospace",
        textTransform: "uppercase",
        letterSpacing: "0.06em",
        cursor: "pointer",
        transition: "border-color 150ms, background 150ms, color 150ms",
        // Active state: brand-tinted pill
        background: enabled ? C.accentSubtle : "transparent",
        color: enabled ? C.accent : C.textDim,
        border: enabled ? `1px solid ${C.borderAccent}` : "1px solid rgba(255,255,255,0.1)",
      }}
    >
      {/* Dot indicator — filled when active */}
      <span style={{ fontSize: "8px", lineHeight: 1 }}>
        {enabled ? "●" : "○"}
      </span>
      heat
    </button>
  );
}
