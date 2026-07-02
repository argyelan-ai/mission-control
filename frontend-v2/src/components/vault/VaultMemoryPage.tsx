"use client";

/**
 * VaultMemoryPage — M.3 T8 — Editorial Codex aesthetic.
 *
 * Reads from vault routes (GET /vault/notes, /vault/search, /vault/note/{path}).
 * Legacy board_memory data remains accessible via LegacyMemoryPage.tsx (M.5 cleanup).
 *
 * URL state: /memory?q=...&scope=...&agent=...&type=...&path=...
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import AppShell from "@/components/layout/AppShell";
import { api } from "@/lib/api";
import type { VaultNote, VaultNoteType } from "@/lib/types";
import type { VaultScope } from "@/hooks/useVaultSearch";
import type { VaultFilterState } from "./VaultFilterStrip";
import { useVaultSearch } from "@/hooks/useVaultSearch";
import { useVaultList } from "@/hooks/useVaultList";
import { useVaultStream } from "@/hooks/useVaultStream";
import { useVoiceHighlight } from "@/hooks/useVoiceHighlight";
import { VoiceHighlightBridge } from "@/components/memory/VoiceHighlightBridge";
import { VaultHeader } from "./VaultHeader";
import { VaultSearch } from "./VaultSearch";
import { VaultFilterStrip } from "./VaultFilterStrip";
import { VaultNotesList } from "./VaultNotesList";
import { VaultReadingPanel } from "./VaultReadingPanel";
import { VaultTrashPage } from "./VaultTrashPage";
import { VaultGraphPage } from "@/components/pages/VaultGraphPage";
import { CreateVaultNoteModal } from "./CreateVaultNoteModal";
import { C } from "@/lib/colors";

// ── View tab ──────────────────────────────────────────────────────────────────

type VaultView = "list" | "graph" | "trash";

const TAB_LABELS: Record<VaultView, string> = {
  list: "Liste",
  graph: "Graph",
  trash: "Papierkorb",
};

function VaultViewTabs({
  view,
  onChange,
  trashCount,
}: {
  view: VaultView;
  onChange: (next: VaultView) => void;
  trashCount: number;
}) {
  return (
    <div className="flex items-center gap-1 mb-5 border-b border-white/5">
      {(["list", "graph", "trash"] as const).map((v) => {
        const active = view === v;
        return (
          <button
            key={v}
            onClick={() => onChange(v)}
            className={`px-4 py-2 text-xs font-mono uppercase tracking-wider transition-colors -mb-px border-b-2 flex items-center gap-2 ${
              active
                ? "text-[var(--color-text-primary)] border-[var(--color-accent)]"
                : "text-[var(--color-text-muted)] border-transparent hover:text-[var(--color-text-primary)]"
            }`}
          >
            {TAB_LABELS[v]}
            {v === "trash" && trashCount > 0 && (
              <span
                className="font-mono rounded-full px-1.5"
                style={{
                  fontSize: "9.5px",
                  background: active ? C.accentSubtle : "rgba(255,255,255,0.06)",
                  color: active ? C.accent : "var(--color-text-muted)",
                  letterSpacing: "0.04em",
                  lineHeight: 1.6,
                }}
              >
                {trashCount}
              </span>
            )}
          </button>
        );
      })}
    </div>
  );
}

// ── VaultMemoryPage ───────────────────────────────────────────────────────────

export default function VaultMemoryPage() {
  const router = useRouter();
  const searchParams = useSearchParams();

  // ── URL state ──
  const urlQ = searchParams.get("q") ?? "";
  const urlScope = (searchParams.get("scope") as VaultScope) || undefined;
  const urlAgent = searchParams.get("agent") || undefined;
  const urlType = (searchParams.get("type") as VaultNoteType) || undefined;
  const urlPath = searchParams.get("path") || undefined;
  // Phase E task-klammer deeplink: ?task=<uuid> filters the list to all
  // notes + wrappers sharing the task. Triggered from the Brain icon on the
  // Tasks page. UUID, not validated here — backend returns 400 on garbage.
  const urlTask = searchParams.get("task") || undefined;
  const rawView = searchParams.get("view");
  const urlView: VaultView =
    rawView === "graph" ? "graph" : rawView === "trash" ? "trash" : "list";

  // ── Local state mirrors URL ──
  const [view, setView] = useState<VaultView>(urlView);
  const [query, setQuery] = useState(urlQ);
  const [filters, setFilters] = useState<VaultFilterState>({
    scope: urlScope,
    agent: urlAgent,
    type: urlType,
  });
  const [selectedPath, setSelectedPath] = useState<string | null>(urlPath ?? null);

  // ── Filter panel toggle ──
  // Default collapsed (mobile AND desktop) — the goal is to give the list back
  // the vertical space the always-on chip rows used to eat. Persisted across
  // visits in localStorage. Active filters stay visible via the collapsed
  // summary inside VaultFilterStrip, so nothing is lost when folded.
  const [filtersOpen, setFiltersOpen] = useState(false);
  useEffect(() => {
    try {
      setFiltersOpen(localStorage.getItem("mc-vault-filters-open") === "1");
    } catch {
      /* localStorage unavailable (private mode / SSR) — stay collapsed */
    }
  }, []);
  const handleFiltersOpenChange = useCallback((next: boolean) => {
    setFiltersOpen(next);
    try {
      localStorage.setItem("mc-vault-filters-open", next ? "1" : "0");
    } catch {
      /* ignore persistence failure */
    }
  }, []);

  // Detect mobile viewport
  const [isMobile, setIsMobile] = useState(false);
  useEffect(() => {
    const mq = window.matchMedia("(max-width: 767px)");
    setIsMobile(mq.matches);
    const handler = (e: MediaQueryListEvent) => setIsMobile(e.matches);
    mq.addEventListener("change", handler);
    return () => mq.removeEventListener("change", handler);
  }, []);

  // ── URL sync helper ──
  const pushUrl = useCallback(
    (overrides: {
      q?: string;
      scope?: string;
      agent?: string;
      type?: string;
      path?: string | null;
      view?: VaultView;
      task?: string | null;
    }) => {
      const params = new URLSearchParams();
      const q = "q" in overrides ? overrides.q : query;
      const scope = "scope" in overrides ? overrides.scope : filters.scope;
      const agent = "agent" in overrides ? overrides.agent : filters.agent;
      const type = "type" in overrides ? overrides.type : filters.type;
      const path = "path" in overrides ? overrides.path : selectedPath;
      const nextView = "view" in overrides ? overrides.view : view;
      const task = "task" in overrides ? overrides.task : urlTask;

      if (q) params.set("q", q);
      if (scope) params.set("scope", scope);
      if (agent) params.set("agent", agent);
      if (type) params.set("type", type);
      if (path) params.set("path", path);
      if (nextView === "graph" || nextView === "trash") params.set("view", nextView);
      if (task) params.set("task", task);

      const qs = params.toString();
      router.replace(qs ? `/memory?${qs}` : "/memory", { scroll: false });
    },
    [query, filters, selectedPath, view, urlTask, router]
  );

  // ── View switcher ──
  function handleViewChange(next: VaultView) {
    setView(next);
    pushUrl({ view: next });
  }

  // ── Event handlers ──
  function handleQueryChange(q: string) {
    setQuery(q);
    pushUrl({ q });
  }

  function handleFilterChange(next: Partial<VaultFilterState>) {
    const merged = { ...filters, ...next };
    setFilters(merged);
    pushUrl({
      scope: merged.scope,
      agent: merged.agent,
      type: merged.type,
    });
  }

  function handleSelectNote(note: VaultNote) {
    setSelectedPath(note.path);
    pushUrl({ path: note.path });
  }

  function handleClosePanel() {
    setSelectedPath(null);
    pushUrl({ path: null });
  }

  function handleWikilinkClick(target: string) {
    // Navigate to the target by setting it as the search query.
    // T8 limitation: stem-only resolution. M.4 will add proper path lookup.
    handleQueryChange(target);
  }

  // ── Voice → Graph/List Bridge ──────────────────────────────────────────────
  // Single source of truth for voice-driven highlights. The bridge mounts
  // unconditionally so a voice command landing while the user is on Liste or
  // Papierkorb still updates state — when they switch to Graph the highlight
  // is already there (within the hook's 30s auto-clear window).
  //
  // For the Liste tab we treat voice as a temporary override of the manual
  // filter strip: voice.agent/voice.type win over user values when present.
  // Tag/project from voice are ignored here (no UI counterparts on Liste).
  const {
    voiceFilter,
    onVoiceHighlight,
    clearVoiceHighlight,
  } = useVoiceHighlight();

  const _voiceAgent =
    voiceFilter && typeof voiceFilter.agent === "string" ? voiceFilter.agent : undefined;
  const _voiceType =
    voiceFilter && typeof voiceFilter.type === "string"
      ? (voiceFilter.type as VaultNoteType)
      : undefined;
  const effectiveAgent = _voiceAgent ?? filters.agent;
  const effectiveType = _voiceType ?? filters.type;

  // ── Data: search or list ──
  const {
    data: searchData,
    isLoading: searchLoading,
    debouncedQ,
  } = useVaultSearch({
    q: query,
    scope: filters.scope,
    agent: effectiveAgent,
    type: effectiveType,
  });

  const {
    notes: listNotes,
    totalCount,
    isLoading: listLoading,
    isError: listError,
    fetchNextPage,
    hasNextPage,
    isFetchingNextPage,
  } = useVaultList({
    scope: filters.scope,
    agent: effectiveAgent,
    type: effectiveType,
  });

  // Phase E task-klammer mode: when `?task=<uuid>` is in the URL, override
  // the normal list with everything that shares the task. Search + filter
  // strip still work as filters ON TOP of the task subset client-side.
  const {
    data: taskRelatedData,
    isLoading: taskLoading,
    isError: taskError,
  } = useQuery({
    queryKey: ["vault", "related", urlTask],
    queryFn: () => api.vault.related(urlTask as string),
    enabled: !!urlTask,
    staleTime: 30_000,
  });
  const isTaskMode = !!urlTask;
  const taskNotes = taskRelatedData?.notes ?? [];

  // Merge: task-mode wins; then search; then list.
  const isSearchMode = debouncedQ.length > 0;
  const notes: VaultNote[] = isTaskMode
    ? taskNotes
    : isSearchMode
    ? (searchData?.hits ?? [])
    : listNotes;
  const isLoading = isTaskMode
    ? taskLoading
    : isSearchMode
    ? searchLoading
    : listLoading;
  const isError = isTaskMode ? taskError : !isSearchMode && listError;

  // Known agent slugs (for filter strip)
  const knownAgents = useMemo(() => {
    const slugs = new Set<string>();
    listNotes.forEach((n) => { if (n.agent) slugs.add(n.agent); });
    return Array.from(slugs).sort();
  }, [listNotes]);

  // Selected note object
  const selectedNote = notes.find((n) => n.path === selectedPath) ?? null;

  // Auto-select the first note on the first render that has rows. Skipped:
  //   • on mobile (the panel is an overlay — auto-opening would block the list)
  //   • when a deeplink path is already in state (?path=… or ?q=…)
  //   • after the user has interacted (closed the panel, switched tabs etc.)
  //
  // didAutoSelectRef captures the "we tried once" semantics — even if the
  // notes list later refetches with different content (e.g. filter change),
  // we don't yank the selection out from under the user.
  const didAutoSelectRef = useRef(false);
  useEffect(() => {
    if (didAutoSelectRef.current) return;
    if (isMobile) return;
    if (notes.length === 0) return; // wait for the list to load
    didAutoSelectRef.current = true;
    if (selectedPath) return; // deeplink / restored state — respect it
    setSelectedPath(notes[0].path);
    pushUrl({ path: notes[0].path });
  }, [notes, selectedPath, isMobile, pushUrl]);

  // Stats for header
  const noteCount = isSearchMode ? notes.length : totalCount;
  const agentCount = knownAgents.length;

  const isGraphView = view === "graph";
  const isTrashView = view === "trash";

  // Trash count for the tab badge — lightweight (no list rendering, just count).
  const { data: trashData } = useQuery({
    queryKey: ["vault", "trash"],
    queryFn: api.vault.trash.list,
    staleTime: 30_000,
  });
  const trashCount = trashData?.count ?? 0;

  // Live-update trash + list when the WS stream emits delete/restore/purge.
  // The graph view has its own listener; this one fires on the list/trash tabs
  // so the badge count + row visibility stay accurate without polling.
  const queryClient = useQueryClient();
  useVaultStream({
    enabled: !isGraphView, // graph already subscribes on its own
    onMessage: (msg) => {
      if (msg.type === "deleted" || msg.type === "restored" || msg.type === "trash_purged") {
        queryClient.invalidateQueries({ queryKey: ["vault"] });
      }
    },
  });

  // ── Prefetch graph payload during idle time ────────────────────────────────
  // If the user is on the Liste tab now they'll likely click Graph soon — kick
  // off the request in the background so the canvas paints instantly when they
  // do. Backend's Redis cache means this is ~10ms after the first cold build.
  // Skip when we're already on the Graph tab (the page hook fires its own fetch).
  useEffect(() => {
    if (isGraphView) return;
    const handle = (window as Window & {
      requestIdleCallback?: (cb: () => void) => void;
    }).requestIdleCallback;
    const kick = () => {
      // Must mirror useVaultGraph's queryKey (incl. similarity_edges=true)
      // so the prefetched entry actually hydrates the graph tab's query.
      queryClient.prefetchQuery({
        queryKey: ["vault", "graph", false, "30d", true],
        queryFn: () => api.vault.graph({ cluster: false, heatmap: "30d", similarity_edges: true }),
        staleTime: 60_000,
      });
    };
    if (typeof handle === "function") {
      handle(kick);
    } else {
      // Safari/older — settle for a short timeout instead of blocking paint.
      const t = setTimeout(kick, 600);
      return () => clearTimeout(t);
    }
  }, [isGraphView, queryClient]);

  return (
    <AppShell fullHeight>
      {/* No inner maxWidth here — AppShell already wraps both modes in
          a 1600px max-w mx-auto container. A second wrap (1400px Liste,
          100% Graph) just made the Liste tab's left-edge shift 100px
          right of Graph's, which the operator called out. Single rail = aligned
          tabs. */}
      <div className="flex flex-col min-h-0 h-full w-full">
        {/* Header */}
        <VaultHeader
          noteCount={noteCount}
          agentCount={agentCount}
          actions={
            <CreateVaultNoteModal
              enabled={!isTrashView}
              onCreated={(newPath) => {
                // Drop the user into Liste with the brand-new note selected
                // so they see their entry immediately. Without this they'd
                // have to scroll the freshly-invalidated list to find it.
                if (view !== "list") {
                  setView("list");
                  pushUrl({ view: "list", path: newPath });
                } else {
                  setSelectedPath(newPath);
                  pushUrl({ path: newPath });
                }
              }}
            />
          }
        />

        {/* View tabs (Liste / Graph / Papierkorb) */}
        <VaultViewTabs view={view} onChange={handleViewChange} trashCount={trashCount} />

        {/* GRAPH VIEW — full-viewport embedded Obsidian-style 2D graph.
            fullHeight=true on AppShell removes the outer overflow-y-auto +
            1600px wrap, so the canvas spans edge-to-edge with no page-scroll. */}
        {isGraphView && (
          <div className="flex-1 min-h-0">
            <VaultGraphPage
              embedded
              voiceFilter={voiceFilter}
              clearVoiceHighlight={clearVoiceHighlight}
            />
          </div>
        )}

        {/* TRASH VIEW — list of soft-deleted notes with restore + purge. */}
        {isTrashView && <VaultTrashPage />}

        {/* LIST VIEW — search + filters + two-column */}
        {view === "list" && (<>

        {/* Phase E task-klammer banner — visible when ?task=<uuid> is set.
            Shows count + "Alle Notes anzeigen" back-link to escape the filter. */}
        {isTaskMode && (
          <div
            className="mb-4 flex items-center justify-between gap-3 px-3 py-2 rounded-md"
            style={{
              background: C.accentSubtle,
              border: `1px solid ${C.borderAccent}`,
            }}
          >
            <div className="flex items-center gap-2.5 min-w-0">
              <span
                className="font-mono uppercase tracking-[0.14em] shrink-0"
                style={{ fontSize: "10px", color: C.accent }}
              >
                task-klammer
              </span>
              <span
                className="text-xs truncate"
                style={{ color: "var(--color-text-body)" }}
              >
                {taskRelatedData?.count ?? 0} Notes + Files zu Task{" "}
                <span className="font-mono opacity-70">{urlTask?.slice(0, 8)}</span>
              </span>
            </div>
            <button
              type="button"
              onClick={() => {
                setSelectedPath(null);
                pushUrl({ task: null, path: null });
              }}
              className="text-xs px-2 py-1 rounded hover:bg-[rgba(255,255,255,0.05)] shrink-0"
              style={{ color: "var(--color-text-muted)" }}
            >
              Alle Notes anzeigen
            </button>
          </div>
        )}

        {/* Voice filter banner — only visible when a voice command set a filter
            that's narrowing the list. Persists until 30s auto-clear or manual
            dismiss. Shown above Search so the user can't miss it. */}
        {voiceFilter && (
          <div
            className="mb-4 flex items-center gap-2.5 px-3 py-2 rounded-md"
            style={{
              background: C.accentSubtle,
              border: `1px solid ${C.borderAccent}`,
            }}
          >
            <span
              className="font-mono uppercase tracking-[0.14em]"
              style={{ fontSize: "10px", color: C.accent }}
            >
              voice highlight
            </span>
            <span
              className="font-mono"
              style={{ fontSize: "11.5px", color: "var(--color-text-secondary)" }}
            >
              {Object.entries(voiceFilter)
                .filter(([, v]) => v !== undefined)
                .map(([k, v]) => `${k}=${Array.isArray(v) ? v.join(",") : v}`)
                .join("  ·  ")}
            </span>
            <button
              type="button"
              onClick={clearVoiceHighlight}
              aria-label="Clear voice highlight"
              className="ml-auto font-mono"
              style={{
                fontSize: "11px",
                color: "var(--color-text-muted)",
                background: "transparent",
                border: "none",
                cursor: "pointer",
                padding: "2px 6px",
              }}
              onMouseEnter={(e) =>
                ((e.currentTarget as HTMLButtonElement).style.color = "var(--color-text-primary)")
              }
              onMouseLeave={(e) =>
                ((e.currentTarget as HTMLButtonElement).style.color = "var(--color-text-muted)")
              }
            >
              dismiss ×
            </button>
          </div>
        )}

        {/* Search */}
        <div className="mb-5">
          <VaultSearch value={query} onChange={handleQueryChange} />
        </div>

        {/* Filters — collapsed by default (toggle inside the strip). Saves the
            ~25% vertical height the always-on chip rows used to consume; the
            freed height flows to the list via the flex layout below. */}
        <div className="mb-5">
          <VaultFilterStrip
            filters={filters}
            agents={knownAgents}
            onChange={handleFilterChange}
            open={filtersOpen}
            onOpenChange={handleFiltersOpenChange}
          />
        </div>

        {/* Two-column layout on md+, single column on mobile.
            flex-1 + min-h-0 lets this stretch into whatever the outer
            flex-col gives us. Previous `calc(100vh - 320px)` was a
            hardcoded subtraction of header + tabs + search + filter
            heights — fragile on smaller viewports and a contributor to
            the "list looks endless, panel disappears" UX.

            The 400px floor is desktop-only (sm:). On mobile the header +
            tabs + search + filter strip already consume ~465px of an
            844px viewport; a hard 400px floor on top of that overflowed
            the fixed-height shell, so `flex-1` collapsed the list to its
            minHeight and pushed its bottom off-screen — the operator saw "only one
            entry you can scroll". Letting the list take the true remaining
            flex height (no floor < sm) fixes the collapse. */}
        <div className="flex flex-1 min-h-0 sm:min-h-[400px]">
          {/* Left: note list */}
          <div
            className="overflow-y-auto overflow-x-hidden shrink-0 min-h-0 scrollbar-none"
            style={{
              width: isMobile ? "100%" : "min(460px, 42%)",
              borderRight: isMobile ? "none" : "1px solid rgba(255,255,255,0.05)",
              scrollbarWidth: "none",
              msOverflowStyle: "none",
            } as React.CSSProperties}
          >
            <VaultNotesList
              notes={notes}
              selectedPath={selectedPath}
              onSelect={handleSelectNote}
              isLoading={isLoading}
              isError={!!isError}
              query={isSearchMode ? debouncedQ : undefined}
              scope={filters.scope}
              hasNextPage={!isSearchMode ? hasNextPage : undefined}
              isFetchingNextPage={!isSearchMode ? isFetchingNextPage : undefined}
              onLoadMore={!isSearchMode ? fetchNextPage : undefined}
            />
          </div>

          {/* Right: reading panel (desktop only — mobile uses overlay) */}
          {!isMobile && (
            <div className="flex-1 min-w-0 overflow-hidden">
              <VaultReadingPanel
                note={selectedNote}
                onWikilinkClick={handleWikilinkClick}
                onSelectNote={(p) => { setSelectedPath(p); pushUrl({ path: p }); }}
                onDeleted={handleClosePanel}
              />
            </div>
          )}
        </div>

        {/* Mobile overlay reading panel */}
        {isMobile && (
          <VaultReadingPanel
            note={selectedNote}
            onClose={handleClosePanel}
            onWikilinkClick={handleWikilinkClick}
            onSelectNote={(p) => { setSelectedPath(p); pushUrl({ path: p }); }}
            onDeleted={handleClosePanel}
            isMobileOverlay
          />
        )}
        </>)}
      </div>

      {/* Voice → graph/list bridge — mounted unconditionally so commands fire
          on every tab. Previously this lived inside VaultGraphPage which only
          mounts on the Graph tab; users on Liste/Papierkorb missed updates. */}
      <VoiceHighlightBridge onHighlight={onVoiceHighlight} />
    </AppShell>
  );
}
