// frontend-v2/src/components/shared/BrowserTabStrip.tsx
"use client";

import { useEffect, useRef } from "react";
import { useTranslations } from "next-intl";
import { C } from "@/lib/colors";
import { StatusDot } from "@/components/shared/StatusDot";

export interface BrowserStripTab {
  id: string;
  title: string;
  url?: string;
}

function tabLabel(tab: BrowserStripTab): string {
  if (tab.title) return tab.title;
  if (tab.url) {
    try {
      return new URL(tab.url).hostname || tab.url;
    } catch {
      return tab.url;
    }
  }
  return tab.id;
}

/**
 * The browser session's open tabs as one scrollable row (ADR-088 tab strip).
 * The shown tab carries the selection (surface + border, DESIGN.md "Aktiv"),
 * the tab the agent works in carries a pulsing "busy" dot — the two can
 * differ when the operator looks at another tab with Follow off.
 */
export function BrowserTabStrip({
  tabs,
  shownId,
  agentTabId,
  agentName,
  onSelect,
}: {
  tabs: BrowserStripTab[];
  shownId: string | null;
  agentTabId: string | null;
  agentName: string;
  onSelect: (id: string) => void;
}) {
  const t = useTranslations("browserLive");
  const shownRef = useRef<HTMLButtonElement | null>(null);
  // The shown tab (in Follow mode: the agent's) is always in view, also when
  // it is the last of many — the strip scrolls, it never hides the selection.
  useEffect(() => {
    shownRef.current?.scrollIntoView?.({ block: "nearest", inline: "nearest" });
  }, [shownId, tabs.length]);
  if (tabs.length === 0) return null;
  return (
    <div
      role="tablist"
      aria-label={t("tabsLabel")}
      data-region="browser-tabs"
      className="flex items-center gap-2 px-3 pb-2 overflow-x-auto shrink-0"
      style={{ scrollbarWidth: "none" }}
    >
      {tabs.map((tab) => {
        const shown = tab.id === shownId;
        const agentHere = tab.id === agentTabId;
        return (
          <button
            key={tab.id}
            ref={shown ? shownRef : undefined}
            role="tab"
            type="button"
            aria-selected={shown}
            onClick={() => onSelect(tab.id)}
            title={tab.url ?? tabLabel(tab)}
            className="min-h-11 max-w-40 shrink-0 flex items-center gap-2 px-3 rounded-md text-xs transition-colors"
            style={
              shown
                ? { background: C.accentSubtle, border: `1px solid ${C.borderAccent}`, color: C.textPrimary }
                : { background: "transparent", border: `1px solid ${C.border}`, color: C.textSecondary }
            }
          >
            {agentHere && (
              <>
                <StatusDot status="busy" size="sm" pulse />
                <span className="sr-only">{t("agentWorkingHere", { name: agentName })}</span>
              </>
            )}
            <span className="truncate">{tabLabel(tab)}</span>
          </button>
        );
      })}
    </div>
  );
}
