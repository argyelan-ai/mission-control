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
import { FileText, GitCompare, Globe } from "lucide-react";
import { useTranslations } from "next-intl";
import { C } from "@/lib/colors";

export type PanelKind = "diff" | "browser" | "doc";

// Labels are i18n keys under `sessions` (the doc label reuses the group
// room's own `groups.resultPanel`, so both places say the same word).
const PANELS: { key: PanelKind; labelKey: string; icon: typeof GitCompare }[] = [
  { key: "diff", labelKey: "panels.diff", icon: GitCompare },
  { key: "browser", labelKey: "panels.browser", icon: Globe },
  // Nur im Gruppenraum: das lebende Ergebnis-Dokument (ADR-075). Ein Agent
  // hat keins, eine Gruppe hat keinen Workspace-Diff — deshalb entscheidet
  // die Seite über `only`, welche Knöpfe hier überhaupt erscheinen.
  { key: "doc", labelKey: "groups.resultPanel", icon: FileText },
];

interface PanelRailProps {
  active: PanelKind | null;
  onSelect: (panel: PanelKind | null) => void;
  /** Auswahl der sichtbaren Panels. Ohne Angabe: Diff + Browser (bisheriges
   *  Verhalten, damit bestehende Aufrufer unverändert bleiben). */
  only?: PanelKind[];
}

export function PanelRail({ active, onSelect, only }: PanelRailProps) {
  const t = useTranslations("sessions");
  const visible = only ?? (["diff", "browser"] as PanelKind[]);
  return (
    <div
      role="toolbar"
      aria-label={t("sectionPanels")}
      // Top-aligned, not centred: two icons floating in the middle of a
      // full-height column read as a mistake. They belong next to the chat
      // header they act on.
      // Kein eigener Rahmen mehr: die Schiene sitzt seit 22.08.2026 INNERHALB
      // der gemeinsamen Chat-Insel. Ein Rahmen im Rahmen las sich als drittes
      // konkurrierendes Kästchen; eine Linie links genügt als Trennung.
      className="hidden md:flex md:flex-col items-center gap-1 md:px-1.5 md:py-3 md:border-l md:overflow-hidden shrink-0"
      style={{ background: C.bgSurface, borderColor: C.border }}
    >
      {PANELS.filter((p) => visible.includes(p.key)).map(({ key, labelKey, icon: Icon }) => {
        const isActive = active === key;
        const label = t(labelKey);
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
