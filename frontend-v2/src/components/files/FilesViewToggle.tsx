"use client";

import { useTranslations } from "next-intl";

import { LayoutGrid, List } from "lucide-react";
import { C } from "@/lib/colors";
import type { ViewMode } from "./FilesBrowser";

export function FilesViewToggle({
  view, onChange,
}: {
  view: ViewMode;
  onChange: (v: ViewMode) => void;
}) {
  const tr = useTranslations("files");
  return (
    <div
      className="inline-flex items-center rounded-lg overflow-hidden shrink-0"
      style={{ border: `1px solid ${C.border}` }}
      role="group"
      aria-label={tr("viewMode")}
    >
      {(["list", "grid"] as const).map((v) => {
        const Icon = v === "list" ? List : LayoutGrid;
        const active = view === v;
        return (
          <button
            key={v}
            onClick={() => onChange(v)}
            aria-pressed={active}
            aria-label={v === "list" ? tr("listView") : tr("gridView")}
            title={v === "list" ? tr("listView") : tr("gridView")}
            className="flex items-center justify-center w-8 h-8 cursor-pointer transition-colors"
            style={{ background: active ? C.accentSubtle : "transparent", color: active ? C.accent : C.textMuted }}
          >
            <Icon size={14} />
          </button>
        );
      })}
    </div>
  );
}
