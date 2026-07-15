"use client";

import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { motion } from "framer-motion";
import { Search, RefreshCw, X } from "lucide-react";
import AppShell from "@/components/layout/AppShell";
import { api } from "@/lib/api";
import type { FsRoot, FsSearchResult } from "@/lib/types";
import { C } from "@/lib/colors";
import { timeAgo } from "@/lib/utils";
import { FilesBrowser, type ViewMode } from "@/components/files/FilesBrowser";
import { FilesActionBar } from "@/components/files/FilesActionBar";
import { FilePreviewPanel } from "@/components/files/FilePreviewPanel";
import { TrashView } from "@/components/files/TrashView";
import { FilesSidebar, TRASH_KEY } from "@/components/files/FilesSidebar";
import { FilesViewToggle } from "@/components/files/FilesViewToggle";
import { FilesSearchFilters, type FilesSearchFilterState } from "@/components/files/FilesSearchFilters";
import { fileIcon, fileIconColor, humanSize, mtimeToIso } from "@/components/files/fileUtils";

// Roots where a filename tells you almost nothing (screenshots, generated
// storyboard frames, misc media) — thumbnails beat a UUID-ish name list.
const GRID_DEFAULT_ROOTS = new Set(["media", "mcp-screenshots", "storyboard-images"]);

function viewStorageKey(rootKey: string) {
  return `mc:files:view:${rootKey}`;
}

// 300ms debounce — matches useVaultSearch.
function useDebounced<T>(value: T, delay: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setDebounced(value), delay);
    return () => clearTimeout(t);
  }, [value, delay]);
  return debounced;
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
  const SEARCH_PAGE_SIZE = 50;
  const [searchPage, setSearchPage] = useState(0);
  // A new query starts back at page 0 — otherwise you'd land on an empty page.
  useEffect(() => setSearchPage(0), [debouncedQuery]);

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

  const [searchFilters, setSearchFilters] = useState<FilesSearchFilterState>({});
  function updateSearchFilters(next: Partial<FilesSearchFilterState>) {
    setSearchFilters((prev) => ({ ...prev, ...next }));
    setSearchPage(0);
  }

  const { data: searchData, isLoading: loadingSearch } = useQuery({
    queryKey: ["files-search", debouncedQuery, searchPage, searchFilters],
    queryFn: () =>
      api.files.search({
        q: debouncedQuery,
        limit: SEARCH_PAGE_SIZE,
        offset: searchPage * SEARCH_PAGE_SIZE,
        type: searchFilters.type,
        agent: searchFilters.agent,
        root: searchFilters.root,
      }),
    enabled: searching,
    placeholderData: (prev) => prev,
  });

  // Agent dropdown only ever offers agents actually present in the current
  // result set — no separate agents endpoint needed for this filter.
  const agentsInResults = useMemo(() => {
    const slugs = new Set<string>();
    for (const r of searchData?.results ?? []) {
      if (r.agent_slug) slugs.add(r.agent_slug);
    }
    return [...slugs].sort();
  }, [searchData]);

  // Grid vs. list is a per-root preference (screenshots want thumbnails,
  // code/vault want a scannable list) — persisted so it survives reloads.
  const [view, setView] = useState<ViewMode>("list");
  useEffect(() => {
    if (!activeRootKey || activeRootKey === TRASH_KEY) return;
    try {
      const stored = localStorage.getItem(viewStorageKey(activeRootKey));
      if (stored === "grid" || stored === "list") {
        setView(stored);
        return;
      }
    } catch {
      // storage unavailable — fall through to the root-type default
    }
    setView(GRID_DEFAULT_ROOTS.has(activeRootKey) ? "grid" : "list");
  }, [activeRootKey]);

  function changeView(next: ViewMode) {
    setView(next);
    if (activeRootKey) {
      try {
        localStorage.setItem(viewStorageKey(activeRootKey), next);
      } catch {
        // best-effort persistence only
      }
    }
  }

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
      <div className="max-w-[1400px] mx-auto">
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
          <>
            <FilesSearchFilters
              filters={searchFilters}
              onChange={updateSearchFilters}
              roots={roots}
              agents={agentsInResults}
            />
            <SearchResults
              results={searchData?.results ?? []}
              loading={loadingSearch}
              roots={roots}
              onOpen={openSearchResult}
            />
            {(searchPage > 0 || searchData?.has_more) && (
              <div className="flex items-center justify-center gap-4 mt-4">
                <button
                  onClick={() => setSearchPage((p) => Math.max(0, p - 1))}
                  disabled={searchPage === 0 || loadingSearch}
                  className="px-3 py-1.5 text-sm rounded-lg cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed"
                  style={{ background: C.bgDeep, border: `1px solid ${C.border}`, color: C.textSecondary }}
                >
                  ← Prev
                </button>
                <span className="text-sm tabular-nums" style={{ color: C.textMuted }}>
                  Page {searchPage + 1}
                </span>
                <button
                  onClick={() => setSearchPage((p) => p + 1)}
                  disabled={!searchData?.has_more || loadingSearch}
                  className="px-3 py-1.5 text-sm rounded-lg cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed"
                  style={{ background: C.bgDeep, border: `1px solid ${C.border}`, color: C.textSecondary }}
                >
                  Next →
                </button>
              </div>
            )}
          </>
        ) : (
          /* ── Finder-style master-detail: root sidebar + browser ── */
          <div className="flex flex-col md:flex-row gap-5">
            <FilesSidebar roots={roots} activeKey={activeRootKey} onSelect={switchRoot} />

            <div className="flex-1 min-w-0">
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
                    view={view}
                    headerActions={<FilesViewToggle view={view} onChange={changeView} />}
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
            </div>
          </div>
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
