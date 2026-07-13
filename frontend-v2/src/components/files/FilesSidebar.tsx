"use client";

import { FolderOpen, Trash2, type LucideIcon } from "lucide-react";
import * as Icons from "lucide-react";
import type { FsRoot } from "@/lib/types";
import { C } from "@/lib/colors";

/** Sentinel root key for the synthetic Trash pseudo-root — kept in sync with
 *  the one page.tsx uses (double-underscore so it never collides with a
 *  real fs_roots slug). */
export const TRASH_KEY = "__trash__";

/** Resolve a backend icon-name hint (PascalCase lucide name) to a component. */
function rootIcon(name: string): LucideIcon {
  const map = Icons as unknown as Record<string, LucideIcon>;
  return map[name] ?? FolderOpen;
}

interface FilesSidebarProps {
  roots: FsRoot[];
  activeKey: string | null;
  onSelect: (key: string) => void;
}

function SidebarEntry({
  icon: Icon, label, count, active, onClick,
}: {
  icon: LucideIcon;
  label: string;
  count?: number;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      className="flex items-center gap-2.5 px-3 py-2 rounded-lg text-sm font-medium transition-colors cursor-pointer w-full text-left shrink-0"
      style={{
        background: active ? C.accentSubtle : "transparent",
        color: active ? C.accent : C.textSecondary,
        border: `1px solid ${active ? C.borderAccent : "transparent"}`,
      }}
      onMouseEnter={(e) => { if (!active) e.currentTarget.style.color = C.textPrimary; }}
      onMouseLeave={(e) => { if (!active) e.currentTarget.style.color = C.textSecondary; }}
    >
      <Icon size={15} style={{ color: active ? C.accent : C.textMuted, flexShrink: 0 }} />
      <span className="truncate flex-1">{label}</span>
      {count !== undefined && (
        <span
          className="text-[10px] tabular-nums px-1.5 py-0.5 rounded-full shrink-0"
          style={{ background: active ? "rgba(15,163,163,0.18)" : C.bgElevated, color: active ? C.accent : C.textMuted }}
        >
          {count}
        </span>
      )}
    </button>
  );
}

/** Left-hand "Finder sidebar" — vertical list of file roots on desktop,
 *  a horizontally-scrollable row on narrow viewports. Trash sits below a
 *  divider so it reads as a separate, less-frequent destination. */
export function FilesSidebar({ roots, activeKey, onSelect }: FilesSidebarProps) {
  return (
    <nav
      aria-label="File roots"
      className="flex md:flex-col gap-1 overflow-x-auto md:overflow-visible pb-1 md:pb-0 md:w-56 shrink-0 tab-strip"
    >
      {roots.map((r) => (
        <SidebarEntry
          key={r.key}
          icon={rootIcon(r.icon)}
          label={r.label}
          count={r.indexed_count}
          active={r.key === activeKey}
          onClick={() => onSelect(r.key)}
        />
      ))}

      {/* Divider — desktop only; mobile keeps it in the same scroll row. */}
      <div className="hidden md:block my-2" style={{ borderTop: `1px solid ${C.borderSubtle}` }} />

      <SidebarEntry
        icon={Trash2}
        label="Trash"
        active={activeKey === TRASH_KEY}
        onClick={() => onSelect(TRASH_KEY)}
      />
    </nav>
  );
}
