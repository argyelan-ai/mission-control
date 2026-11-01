"use client";

/**
 * The side panels next to the chat and the two header buttons that open them.
 *
 * Until ADR-099 (chat workspace, operator decision 01.11.2026) the buttons
 * stood in their own 58 px strip right of the chat (`PanelRail`). They now sit
 * in the desktop chat header, where the eye already is; the strip is gone.
 *
 * Clicking the active button closes the panel again (the chat goes full
 * width) — no separate collapse control.
 *
 * Desktop only (`hidden md:flex`). On the phone the same panels are reached
 * from the header's ⋯ sheet (ChatOptionsSheet) and open full-screen: DESIGN.md
 * K4 allows one action next to ⋯ in a phone title row, and that is the mic.
 */
import { FileText, GitCompare, Globe } from "lucide-react";
import { useTranslations } from "next-intl";
import { C } from "@/lib/colors";

export type PanelKind = "diff" | "browser" | "doc";

// Labels are i18n keys under `sessions` (the doc label reuses the group
// room's own `groups.resultPanel`, so both places say the same word).
export const PANEL_DEFS: { key: PanelKind; labelKey: string; icon: typeof GitCompare }[] = [
  { key: "diff", labelKey: "panels.diff", icon: GitCompare },
  { key: "browser", labelKey: "panels.browser", icon: Globe },
  // Group room only: the living result document (ADR-075). Its button lives
  // in the group header (GroupChatView); listed here so every panel kind has
  // one label and one icon.
  { key: "doc", labelKey: "groups.resultPanel", icon: FileText },
];

interface HeaderPanelButtonsProps {
  active: PanelKind | null;
  onToggle: (panel: PanelKind | null) => void;
  /** Which panels this chat offers. Default: changes + browser. */
  only?: PanelKind[];
}

export function HeaderPanelButtons({ active, onToggle, only }: HeaderPanelButtonsProps) {
  const t = useTranslations("sessions");
  const visible = only ?? (["diff", "browser"] as PanelKind[]);
  return (
    <div
      role="group"
      aria-label={t("sectionPanels")}
      data-testid="header-panel-buttons"
      className="hidden md:flex items-center gap-1 shrink-0"
    >
      {PANEL_DEFS.filter((p) => visible.includes(p.key)).map(({ key, labelKey, icon: Icon }) => {
        const isActive = active === key;
        const label = t(labelKey);
        return (
          <button
            key={key}
            type="button"
            onClick={() => onToggle(isActive ? null : key)}
            aria-pressed={isActive}
            aria-label={label}
            title={label}
            data-testid={`header-panel-${key}`}
            className="flex items-center justify-center w-8 h-8 rounded-md transition-colors cursor-pointer hover:bg-[var(--color-bg-hover)]"
            style={{
              color: isActive ? C.accent : C.textMuted,
              // Pressed = accent tint + accent frame (DESIGN.md "Kein-Halo"):
              // the same signal the strip carried. The resting state gets no
              // inline background, so the hover class can still show.
              ...(isActive ? { background: C.accentSubtle } : null),
              border: `1px solid ${isActive ? C.borderAccent : "transparent"}`,
            }}
          >
            <Icon size={15} />
          </button>
        );
      })}
    </div>
  );
}
