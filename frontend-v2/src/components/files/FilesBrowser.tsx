"use client";

import { useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ChevronRight, ChevronUp, ChevronDown, Loader2, AlertCircle, FolderOpen } from "lucide-react";
import { api } from "@/lib/api";
import type { FsEntry, FsRoot } from "@/lib/types";
import { C } from "@/lib/colors";
import { timeAgo } from "@/lib/utils";
import { colorForAgent } from "@/components/vault/agentColors";
import { fileIcon, fileIconColor, humanSize, mtimeToIso } from "./fileUtils";

export type SortKey = "name" | "size" | "mtime";
export type SortDir = "asc" | "desc";

/** Folders always group above files; sort within each group by the active column. */
export function sortEntries(entries: FsEntry[], key: SortKey, dir: SortDir): FsEntry[] {
  const factor = dir === "asc" ? 1 : -1;
  return entries.slice().sort((a, b) => {
    if (a.is_directory !== b.is_directory) return a.is_directory ? -1 : 1;
    let cmp: number;
    if (key === "size") cmp = a.size - b.size;
    else if (key === "mtime") cmp = a.mtime - b.mtime;
    else cmp = a.name.localeCompare(b.name);
    // stable tiebreak on name so toggling direction never looks random
    if (cmp === 0) cmp = a.name.localeCompare(b.name);
    return cmp * factor;
  });
}

interface FilesBrowserProps {
  root: FsRoot;
  subpath: string;
  /** Navigate into a folder (relative subpath under the active root). */
  onNavigate: (subpath: string) => void;
  /** Open a file in the preview panel (relative subpath under the active root). */
  onSelectFile: (subpath: string) => void;
  /** Currently-previewed file subpath (highlighted row). */
  selectedSubpath?: string | null;
  /** Multi-select set of file subpaths (relative to the active root). */
  selected: Set<string>;
  /** Toggle a single file's selection. */
  onToggleSelect: (subpath: string, on: boolean) => void;
  /** Toggle every file in the current directory at once. */
  onToggleSelectAll: (subpaths: string[], on: boolean) => void;
}

export function FilesBrowser({
  root, subpath, onNavigate, onSelectFile, selectedSubpath,
  selected, onToggleSelect, onToggleSelectAll,
}: FilesBrowserProps) {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["files-list", root.key, subpath],
    queryFn: () => api.files.list(root.key, subpath || undefined),
    refetchInterval: 30_000,
  });

  const parts = subpath ? subpath.split("/") : [];

  function navigateToBreadcrumb(index: number) {
    if (index < 0) onNavigate("");
    else onNavigate(parts.slice(0, index + 1).join("/"));
  }

  // Newest-first by default — that's what you're looking for right after an
  // agent finishes a task, far more often than alphabetical order.
  const [sort, setSort] = useState<{ key: SortKey; dir: SortDir }>({ key: "mtime", dir: "desc" });

  function toggleSort(key: SortKey) {
    setSort((s) => (s.key === key ? { key, dir: s.dir === "asc" ? "desc" : "asc" } : { key, dir: "asc" }));
  }

  const entries = sortEntries(data?.entries ?? [], sort.key, sort.dir);

  // Subpaths of the files (not folders) in the current directory — the unit of
  // selection. Folders are not selectable in v1.
  const fileSubpaths = entries
    .filter((e) => !e.is_directory)
    .map((e) => (subpath ? `${subpath}/${e.name}` : e.name));
  const selectedHere = fileSubpaths.filter((p) => selected.has(p));
  const allSelected = fileSubpaths.length > 0 && selectedHere.length === fileSubpaths.length;
  const someSelected = selectedHere.length > 0 && !allSelected;

  const selectAllRef = useRef<HTMLInputElement | null>(null);

  return (
    <div className="rounded-2xl overflow-hidden" style={{ background: "rgba(255,255,255,0.03)", border: `1px solid ${C.border}` }}>
      {/* Breadcrumb */}
      <div
        className="flex items-center gap-1 flex-wrap px-4 py-3"
        style={{ fontSize: 12, color: C.textMuted, borderBottom: `1px solid ${C.borderSubtle}` }}
      >
        <button
          onClick={() => navigateToBreadcrumb(-1)}
          className="hover:underline cursor-pointer font-medium"
          style={{ color: parts.length > 0 ? C.accent : C.textPrimary }}
        >
          {root.label}
        </button>
        {parts.map((part, i) => (
          <span key={i} className="flex items-center gap-1">
            <ChevronRight size={11} style={{ color: C.textDim }} />
            <button
              onClick={() => navigateToBreadcrumb(i)}
              className="hover:underline cursor-pointer"
              style={{ color: i === parts.length - 1 ? C.textPrimary : C.accent }}
            >
              {part}
            </button>
          </span>
        ))}
      </div>

      {isLoading ? (
        <div className="flex items-center justify-center py-16">
          <Loader2 size={18} className="animate-spin" style={{ color: C.accent }} />
        </div>
      ) : isError || !data ? (
        <div className="flex items-center gap-2 px-4 py-12 justify-center">
          <AlertCircle size={16} style={{ color: C.error }} />
          <span className="text-sm" style={{ color: C.textMuted }}>Failed to load directory</span>
        </div>
      ) : entries.length === 0 ? (
        <div className="flex flex-col items-center justify-center gap-2 py-16">
          <FolderOpen size={28} style={{ color: C.textDim }} />
          <p className="text-sm" style={{ color: C.textMuted }}>This folder is empty</p>
        </div>
      ) : (
        <table className="w-full">
          <thead>
            <tr style={{ borderBottom: `1px solid ${C.borderSubtle}` }}>
              <th className="px-4 py-2.5 w-9">
                <input
                  ref={(el) => {
                    selectAllRef.current = el;
                    if (el) el.indeterminate = someSelected;
                  }}
                  type="checkbox"
                  checked={allSelected}
                  disabled={fileSubpaths.length === 0}
                  aria-label="Select all files"
                  onChange={(e) => onToggleSelectAll(fileSubpaths, e.target.checked)}
                  className="cursor-pointer disabled:cursor-not-allowed"
                  style={{ accentColor: C.accent }}
                />
              </th>
              <SortHeader label="Name" col="name" sort={sort} onToggle={toggleSort} align="left" />
              <SortHeader label="Size" col="size" sort={sort} onToggle={toggleSort} align="right" className="hidden sm:table-cell" />
              <SortHeader label="Modified" col="mtime" sort={sort} onToggle={toggleSort} align="right" className="hidden sm:table-cell" />
            </tr>
          </thead>
          <tbody>
            {entries.map((entry) => (
              <FileRow
                key={entry.name}
                entry={entry}
                subpath={subpath}
                selected={selectedSubpath === (subpath ? `${subpath}/${entry.name}` : entry.name)}
                checked={selected.has(subpath ? `${subpath}/${entry.name}` : entry.name)}
                onNavigate={onNavigate}
                onSelectFile={onSelectFile}
                onToggleSelect={onToggleSelect}
              />
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

/** Small mono chip identifying the agent that produced a deliverable. Reuses
 *  the stable slug→hue hash so the same agent always gets the same color,
 *  matching the Vault/Memory identity dots. */
function AgentBadge({ slug }: { slug: string }) {
  const color = colorForAgent(slug);
  return (
    <span
      className="inline-flex items-center shrink-0 font-mono text-[10px] px-1.5 py-0.5 rounded"
      style={{ background: `${color}1A`, color }}
      title={`Agent: ${slug}`}
    >
      {slug}
    </span>
  );
}

/** Display label for a folder/file: `display_name` (e.g. a task title) as the
 *  primary label with the raw name (often a task UUID) shown small and muted
 *  underneath — full name always available via `title` for a11y/tooltips. */
function EntryLabel({ entry }: { entry: FsEntry }) {
  if (entry.display_name) {
    return (
      <div className="min-w-0">
        <div className="text-sm truncate" style={{ color: C.textPrimary }} title={entry.display_name}>
          {entry.display_name}
        </div>
        <div className="text-[11px] font-mono truncate" style={{ color: C.textDim }} title={entry.name}>
          {entry.name}
        </div>
      </div>
    );
  }
  return (
    <span className="text-sm truncate" style={{ color: C.textPrimary }}>
      {entry.name}
    </span>
  );
}

function FileRow({
  entry, subpath, selected, checked, onNavigate, onSelectFile, onToggleSelect,
}: {
  entry: FsEntry;
  subpath: string;
  selected: boolean;
  checked: boolean;
  onNavigate: (subpath: string) => void;
  onSelectFile: (subpath: string) => void;
  onToggleSelect: (subpath: string, on: boolean) => void;
}) {
  const entrySubpath = subpath ? `${subpath}/${entry.name}` : entry.name;
  const Icon = fileIcon(entry.name, entry.is_directory);
  const color = fileIconColor(entry.name, entry.is_directory);

  return (
    <tr
      className="transition-colors cursor-pointer"
      style={{ borderBottom: `1px solid ${C.borderSubtle}`, background: selected ? C.accentSubtle : "transparent" }}
      onClick={() => entry.is_directory ? onNavigate(entrySubpath) : onSelectFile(entrySubpath)}
      onMouseEnter={(e) => { if (!selected) e.currentTarget.style.background = "rgba(255,255,255,0.02)"; }}
      onMouseLeave={(e) => { if (!selected) e.currentTarget.style.background = "transparent"; }}
    >
      {/* Selection — the cell swallows clicks so toggling never opens/navigates. */}
      <td className="px-4 py-2.5 w-9" onClick={(e) => e.stopPropagation()}>
        {entry.is_directory ? null : (
          <input
            type="checkbox"
            checked={checked}
            aria-label={`Select ${entry.name}`}
            onChange={(e) => onToggleSelect(entrySubpath, e.target.checked)}
            className="cursor-pointer"
            style={{ accentColor: C.accent }}
          />
        )}
      </td>
      <td className="px-4 py-2.5">
        <div className="flex items-center gap-2.5 min-w-0">
          <Icon size={15} style={{ color, flexShrink: 0 }} />
          <EntryLabel entry={entry} />
          {entry.agent_slug && <AgentBadge slug={entry.agent_slug} />}
          {entry.is_directory && <ChevronRight size={13} style={{ color: C.textDim, flexShrink: 0 }} />}
        </div>
      </td>
      <td className="px-4 py-2.5 text-right text-xs tabular-nums hidden sm:table-cell" style={{ color: C.textMuted }}>
        {entry.is_directory ? "—" : humanSize(entry.size)}
      </td>
      <td className="px-4 py-2.5 text-right text-xs hidden sm:table-cell" style={{ color: C.textMuted }}>
        {timeAgo(mtimeToIso(entry.mtime))}
      </td>
    </tr>
  );
}

function SortHeader({
  label, col, sort, onToggle, align, className = "",
}: {
  label: string;
  col: SortKey;
  sort: { key: SortKey; dir: SortDir };
  onToggle: (k: SortKey) => void;
  align: "left" | "right";
  className?: string;
}) {
  const active = sort.key === col;
  const Arrow = sort.dir === "asc" ? ChevronUp : ChevronDown;
  return (
    <th
      className={`px-4 py-2.5 text-[11px] font-semibold uppercase tracking-wider select-none cursor-pointer ${align === "right" ? "text-right" : "text-left"} ${className}`}
      style={{ color: active ? C.textSecondary : C.textMuted }}
      onClick={() => onToggle(col)}
      aria-sort={active ? (sort.dir === "asc" ? "ascending" : "descending") : "none"}
    >
      <span className="inline-flex items-center gap-1">
        {label}
        <Arrow size={12} style={{ color: active ? C.accent : "transparent" }} />
      </span>
    </th>
  );
}
