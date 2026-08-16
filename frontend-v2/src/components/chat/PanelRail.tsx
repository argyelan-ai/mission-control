"use client";

/**
 * PanelRail — Task B6 (revised: Terminal moved to a ChatView center-view
 * toggle instead of living here — see ChatView.tsx's CenterView). Thin icon
 * rail toggling the side panel next to the chat: Diff (placeholder —
 * DiffPanel itself is task C1) and Browser.
 *
 * "Collapsible": clicking the already-active icon sets `active` back to
 * `null`, which collapses the panel slot entirely (chat goes full-width) —
 * no separate chevron/collapse control needed.
 *
 * Responsive via Tailwind `md:` variants in one markup block instead of two
 * parallel renders: desktop is its own slim floating island (rounded-xl,
 * bordered on all sides, gap from its neighbors comes from the parent flex
 * row's `md:gap-2`) beside the panel slot; mobile (<768px) stays a fixed
 * bottom bar, unchanged — the "bottom-sheet trigger" row for the full-screen
 * panel overlay the parent page renders.
 */
import { GitCompare, Globe } from "lucide-react";
import { C } from "@/lib/colors";

export type PanelKind = "diff" | "browser";

const PANELS: { key: PanelKind; label: string; icon: typeof GitCompare }[] = [
  { key: "diff", label: "Diff", icon: GitCompare },
  { key: "browser", label: "Browser", icon: Globe },
];

interface PanelRailProps {
  active: PanelKind | null;
  onSelect: (panel: PanelKind | null) => void;
}

export function PanelRail({ active, onSelect }: PanelRailProps) {
  return (
    <div
      role="toolbar"
      aria-label="Panels"
      className="flex md:flex-col items-center justify-center gap-1 px-2 py-1.5 md:px-1.5 md:py-3 fixed inset-x-0 bottom-0 z-30 md:static md:inset-auto border-t md:border md:rounded-xl md:overflow-hidden shrink-0"
      style={{ background: C.bgSurface, borderColor: C.border }}
    >
      {PANELS.map(({ key, label, icon: Icon }) => {
        const isActive = active === key;
        return (
          <button
            key={key}
            type="button"
            onClick={() => onSelect(isActive ? null : key)}
            aria-pressed={isActive}
            aria-label={label}
            title={label}
            className="flex items-center justify-center w-11 h-11 rounded-md transition-colors cursor-pointer"
            style={{
              background: isActive ? C.accentSubtle : "transparent",
              color: isActive ? C.accent : C.textMuted,
              border: `1px solid ${isActive ? C.borderAccent : "transparent"}`,
            }}
          >
            <Icon size={16} />
          </button>
        );
      })}
    </div>
  );
}
