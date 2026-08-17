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
 * Desktop only (`hidden md:flex`). It used to double as a `fixed bottom-0`
 * bar on mobile, which sat exactly on top of the app's own bottom tab bar —
 * that bar is an in-flow flex child of the shell (deliberately not `fixed`,
 * because iOS gets `fixed` wrong in standalone mode), so a fixed overlay at
 * bottom-0 covers it every time. On mobile the same two panels are reached
 * from the chat header's options sheet instead (ChatOptionsSheet), and open as
 * full-screen sheets — the phone has no room for a permanent rail anyway.
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
      // Top-aligned, not centred: two icons floating in the middle of a
      // full-height column read as a mistake. They belong next to the chat
      // header they act on.
      className="hidden md:flex md:flex-col items-center gap-1 md:px-1.5 md:py-3 md:border md:rounded-xl md:overflow-hidden shrink-0"
      style={{ background: C.bgDeep, borderColor: C.borderActive }}
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
