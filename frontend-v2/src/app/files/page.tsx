"use client";

import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { motion } from "framer-motion";
import {
  Search, RefreshCw, X, FolderOpen, Trash2, type LucideIcon,
} from "lucide-react";
import * as Icons from "lucide-react";
import AppShell from "@/components/layout/AppShell";
import { api } from "@/lib/api";
import type { FsRoot, FsSearchResult } from "@/lib/types";
import { C } from "@/lib/colors";
import { timeAgo } from "@/lib/utils";
import { FilesBrowser } from "@/components/files/FilesBrowser";
import { FilesActionBar } from "@/components/files/FilesActionBar";
import { FilePreviewPanel } from "@/components/files/FilePreviewPanel";
import { TrashView } from "@/components/files/TrashView";
import { fileIcon, fileIconColor, humanSize, mtimeToIso } from "@/components/files/fileUtils";

/** Sentinel root key for the synthetic Trash pseudo-root. Double-underscore
 *  so it can never collide with a real fs_roots slug (all simple slugs). */
const TRASH_KEY = "__trash__";

// 300ms debounce — matches useVaultSearch.
function useDebounced<T>(value: T, delay: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setDebounced(value), delay);
    return () => clearTimeout(t);
  }, [value, delay]);
  return debounced;
}

/** Resolve a backend icon-name hint (PascalCase lucide name) to a component. */
function rootIcon(name: string): LucideIcon {
  const map = Icons as unknown as Record<string, LucideIcon>;
  return map[name] ?? FolderOpen;
}

export default function FilesPage() {
  const [activeRootKey, setActiveRootKey] = useState<string | null>(null);
  const [subpath, setSubpath] = useState("");
  const [selectedFile, setSelectedFile] = useState<string | null>(null);
  // Multi-select set of file subpaths (relative to the active root). Owned by
  // the page so it survives FilesBrowser re-renders and clears on context change.
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [query, setQuery] = useState("");
  const debouncedQuery = useDebounced(query.trim(), 300);

  const { data: rootsData, isLoading: loadingRoots } = useQuery({
    queryKey: ["files-roots"],
    queryFn: () => api.files.roots(),
  });

  const roots = useMemo(() => rootsData?.roots ?? [], [rootsData]);

  // Default to the first root once loaded.
  useEffect(() => {
    if (!activeRootKey && roots.length > 0) setActiveRootKey(roots[0].key);
  }, [roots, activeRootKey]);

  const activeRoot: FsRoot | undefined = useMemo(
    () => roots.find((r) => r.key === activeRootKey),
    [roots, activeRootKey],
  );

  const searching = debouncedQuery.length > 0;

  const { data: searchData, isLoading: loadingSearch } = useQuery({
    queryKey: ["files-search", debouncedQuery],
    queryFn: () => api.files.search({ q: debouncedQuery, limit: 100 }),
    enabled: searching,
  });

  // The index refreshes on a 10-min cadence, so a freshly written file (e.g. a
  // just-registered deliverable) isn't searchable until the next walk. Give the
  // operator a manual trigger + refresh all file views on completion.
  const queryClient = useQueryClient();
  const reindex = useMutation({
    mutationFn: () => api.files.reindex(),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["files-roots"] });
      queryClient.invalidateQueries({ queryKey: ["files-search"] });
      queryClient.invalidateQueries({ queryKey: ["files-list"] });
    },
  });

  function switchRoot(key: string) {
    setActiveRootKey(key);
    setSubpath("");
    setSelectedFile(null);
    setSelected(new Set());
  }

  function toggleSelect(sub: string, on: boolean) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (on) next.add(sub);
      else next.delete(sub);
      return next;
    });
  }

  function toggleSelectAll(subs: string[], on: boolean) {
    setSelected((prev) => {
      const next = new Set(prev);
      for (const s of subs) {
        if (on) next.add(s);
        else next.delete(s);
      }
      return next;
    });
  }

  function openSearchResult(r: FsSearchResult) {
    setActiveRootKey(r.root);
    // Land in the file's parent directory and open the preview on the file.
    const dir = r.rel_path.includes("/") ? r.rel_path.slice(0, r.rel_path.lastIndexOf("/")) : "";
    setSubpath(dir);
    setSelectedFile(r.rel_path);
    setQuery("");
  }

  // The preview panel resolves files against the file's own root. When a
  // search result lands us on a different root, the panel uses activeRoot —
  // which we've just switched to r.root, so it stays consistent.

  return (
    <AppShell>
      <div className="max-w-6xl mx-auto">
        {/* Header */}
        <div className="flex items-end justify-between mb-6 gap-4">
          <div>
            <h1 className="text-2xl font-bold tracking-tight" style={{ color: C.textPrimary }}>
              Files
            </h1>
            <p className="text-sm mt-1" style={{ color: C.textMuted }}>
              Search deliverables, workspaces, vault, and more
            </p>
          </div>
          <button
            onClick={() => reindex.mutate()}
            disabled={reindex.isPending}
            className="flex items-center gap-2 px-3 py-2 text-sm rounded-lg cursor-pointer disabled:opacity-60"
            style={{ background: C.bgDeep, border: `1px solid ${C.border}`, color: C.textSecondary }}
            title="Rescan the file index now — makes freshly written files searchable without waiting for the 10-min auto-walk"
            aria-label="Reindex files"
          >
            <RefreshCw size={14} className={reindex.isPending ? "animate-spin" : ""} style={{ color: C.accent }} />
            {reindex.isPending ? "Reindexing…" : "Reindex"}
          </button>
        </div>

        {/* Search */}
        <div className="relative mb-5">
          <Search size={15} className="absolute left-3 top-1/2 -translate-y-1/2 pointer-events-none" style={{ color: C.textMuted }} />
          <input
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search files…"
            aria-label="Search files"
            className="w-full pl-9 pr-9 py-2.5 text-sm rounded-xl outline-none"
            style={{ background: C.bgDeep, border: `1px solid ${C.border}`, color: C.textPrimary }}
            onFocus={(e) => (e.currentTarget.style.borderColor = C.borderAccent)}
            onBlur={(e) => (e.currentTarget.style.borderColor = C.border)}
          />
          {query && (
            <button
              onClick={() => setQuery("")}
              className="absolute right-3 top-1/2 -translate-y-1/2 cursor-pointer"
              style={{ color: C.textMuted }}
              aria-label="Clear search"
            >
              <X size={15} />
            </button>
          )}
        </div>

        {loadingRoots ? (
          <div className="flex items-center justify-center h-64">
            <RefreshCw size={20} className="animate-spin" style={{ color: C.accent }} />
          </div>
        ) : searching ? (
          /* ── Search results ── */
          <SearchResults
            results={searchData?.results ?? []}
            loading={loadingSearch}
            roots={roots}
            onOpen={openSearchResult}
          />
        ) : (
          <>
            {/* Root selector — tab strip */}
            <div className="flex gap-1 mb-5 overflow-x-auto tab-strip pb-1">
              {roots.map((r) => {
                const Icon = rootIcon(r.icon);
                const active = r.key === activeRootKey;
                return (
                  <button
                    key={r.key}
                    onClick={() => switchRoot(r.key)}
                    className="flex items-center gap-2 px-3 py-2 rounded-lg text-sm font-medium whitespace-nowrap transition-colors cursor-pointer shrink-0"
                    style={{
                      background: active ? C.accentSubtle : "transparent",
                      color: active ? C.accent : C.textSecondary,
                      border: `1px solid ${active ? C.borderAccent : "transparent"}`,
                    }}
                    onMouseEnter={(e) => { if (!active) e.currentTarget.style.color = C.textPrimary; }}
                    onMouseLeave={(e) => { if (!active) e.currentTarget.style.color = C.textSecondary; }}
                  >
                    <Icon size={15} style={{ color: active ? C.accent : C.textMuted }} />
                    {r.label}
                    <span
                      className="text-[10px] tabular-nums px-1.5 py-0.5 rounded-full"
                      style={{ background: C.bgElevated, color: C.textMuted }}
                    >
                      {r.indexed_count}
                    </span>
                  </button>
                );
              })}

              {/* Synthetic Trash pseudo-root — not in /roots, never an FsRoot */}
              {(() => {
                const active = activeRootKey === TRASH_KEY;
                return (
                  <button
                    onClick={() => switchRoot(TRASH_KEY)}
                    className="flex items-center gap-2 px-3 py-2 rounded-lg text-sm font-medium whitespace-nowrap transition-colors cursor-pointer shrink-0"
                    style={{
                      background: active ? C.accentSubtle : "transparent",
                      color: active ? C.accent : C.textSecondary,
                      border: `1px solid ${active ? C.borderAccent : "transparent"}`,
                    }}
                    onMouseEnter={(e) => { if (!active) e.currentTarget.style.color = C.textPrimary; }}
                    onMouseLeave={(e) => { if (!active) e.currentTarget.style.color = C.textSecondary; }}
                  >
                    <Trash2 size={15} style={{ color: active ? C.accent : C.textMuted }} />
                    Trash
                  </button>
                );
              })()}
            </div>

            {activeRootKey === TRASH_KEY ? (
              <motion.div
                key={TRASH_KEY}
                initial={{ opacity: 0, y: 8 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ duration: 0.2 }}
              >
                <TrashView />
              </motion.div>
            ) : activeRoot && (
              <motion.div
                key={activeRoot.key}
                initial={{ opacity: 0, y: 8 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ duration: 0.2 }}
              >
                <FilesBrowser
                  root={activeRoot}
                  subpath={subpath}
                  onNavigate={(p) => { setSubpath(p); setSelectedFile(null); setSelected(new Set()); }}
                  onSelectFile={setSelectedFile}
                  selectedSubpath={selectedFile}
                  selected={selected}
                  onToggleSelect={toggleSelect}
                  onToggleSelectAll={toggleSelectAll}
                />
              </motion.div>
            )}
          </>
        )}
      </div>

      {/* Floating multi-select action bar */}
      {activeRoot && selected.size > 0 && (
        <FilesActionBar
          root={activeRoot}
          selected={selected}
          onClear={() => setSelected(new Set())}
        />
      )}

      {/* Preview slide-over */}
      {activeRoot && (
        <FilePreviewPanel
          root={activeRoot}
          subpath={selectedFile}
          onClose={() => setSelectedFile(null)}
        />
      )}
    </AppShell>
  );
}

// ── Search results table ──────────────────────────────────────────────────────

function SearchResults({
  results, loading, roots, onOpen,
}: {
  results: FsSearchResult[];
  loading: boolean;
  roots: FsRoot[];
  onOpen: (r: FsSearchResult) => void;
}) {
  const rootLabel = (key: string) => roots.find((r) => r.key === key)?.label ?? key;

  if (loading) {
    return (
      <div className="flex items-center justify-center h-48">
        <RefreshCw size={18} className="animate-spin" style={{ color: C.accent }} />
      </div>
    );
  }

  if (results.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center gap-2 py-16">
        <Search size={28} style={{ color: C.textDim }} />
        <p className="text-sm" style={{ color: C.textMuted }}>No results</p>
      </div>
    );
  }

  return (
    <div className="rounded-2xl overflow-hidden" style={{ background: "rgba(255,255,255,0.03)", border: `1px solid ${C.border}` }}>
      <div className="px-4 py-3" style={{ borderBottom: `1px solid ${C.borderSubtle}` }}>
        <span className="text-xs font-semibold uppercase tracking-wider" style={{ color: C.textSecondary }}>
          {results.length} results
        </span>
      </div>
      <div className="divide-y" style={{ borderColor: C.borderSubtle }}>
        {results.map((r, i) => {
          const Icon = fileIcon(r.name, false);
          const color = fileIconColor(r.name, false);
          return (
            <button
              key={`${r.root}:${r.rel_path}:${i}`}
              onClick={() => onOpen(r)}
              className="flex items-center gap-3 px-4 py-2.5 w-full text-left transition-colors cursor-pointer"
              onMouseEnter={(e) => (e.currentTarget.style.background = "rgba(255,255,255,0.02)")}
              onMouseLeave={(e) => (e.currentTarget.style.background = "transparent")}
            >
              <Icon size={15} style={{ color, flexShrink: 0 }} />
              <div className="flex-1 min-w-0">
                <div className="text-sm truncate" style={{ color: C.textPrimary }}>{r.name}</div>
                <div className="text-xs font-mono truncate" style={{ color: C.textMuted }}>
                  {rootLabel(r.root)} · {r.rel_path}
                </div>
              </div>
              <div className="flex items-center gap-3 shrink-0 text-xs" style={{ color: C.textMuted }}>
                <span className="tabular-nums hidden sm:inline">{humanSize(r.size)}</span>
                <span className="hidden sm:inline">{timeAgo(mtimeToIso(r.mtime))}</span>
              </div>
            </button>
          );
        })}
      </div>
    </div>
  );
}
