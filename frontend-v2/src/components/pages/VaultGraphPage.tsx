"use client";

/**
 * VaultGraphPage — M.4 T10 orchestrator for the /memory/graph route.
 *
 * Assembles:
 *   T6  MemoryGraph2D        — Obsidian-style 2D canvas graph (SSR-disabled via dynamic import)
 *   T7  ClusterOverlay       — SVG convex-hull cluster labels
 *   T8  (ActivityHeatmap)    — heatmap toggle state lives here; passed to MemoryGraph2D
 *   T9  GraphFilterSidebar   — left rail, filter state lifted here
 *   T9  NoteSidePanel        — slide-in right panel on node click
 *   T9  VoiceHighlightBridge — headless WS subscriber
 *   T9  TraversalAnimation   — headless wikilink traversal coordinator
 *   T9  useVoiceHighlight    — auto-clearing voice filter state
 *
 * Plus new hooks:
 *   T10 useVaultGraph        — TanStack Query for GET /vault/graph
 *   T10 useVaultStream       — WebSocket for live note changes
 *
 * State: all lifted here; children are presentational.
 * Voice filter overrides manual filter for camera zoom but does NOT replace
 * the user's manual filter — cleared independently via the badge dismiss button.
 */

import { useState, useRef, useMemo, useCallback, useEffect } from "react";
import dynamic from "next/dynamic";
import { useQueryClient } from "@tanstack/react-query";

import type { MemoryGraph2DRef } from "@/components/memory/MemoryGraph2D";
import { GraphFilterSidebar } from "@/components/memory/GraphFilterSidebar";
import { NoteSidePanel } from "@/components/memory/NoteSidePanel";
import { VoiceHighlightBridge } from "@/components/memory/VoiceHighlightBridge";
import { TraversalAnimation } from "@/components/memory/TraversalAnimation";

import { useVaultGraph } from "@/hooks/useVaultGraph";
import { useVaultStream } from "@/hooks/useVaultStream";
import { useVoiceHighlight } from "@/hooks/useVoiceHighlight";

import type { GraphFilter, GraphNode } from "@/lib/types";
import { C } from "@/lib/colors";

// When the parent (VaultMemoryPage) passes voice-highlight props, we use
// those instead of the internal hook so the bridge is shared across tabs.
// When called standalone (tests, /memory/graph route — currently a redirect),
// we fall back to self-contained state + an internal bridge.

// ── SSR-safe dynamic import (canvas + rAF need browser globals) ──────────────

const MemoryGraph2D = dynamic(
  () => import("@/components/memory/MemoryGraph2D").then((m) => m.MemoryGraph2D),
  {
    ssr: false,
    loading: () => (
      <div className="flex items-center justify-center h-full text-[var(--color-text-secondary)] font-mono text-sm">
        Loading graph…
      </div>
    ),
  }
);

// ── Filter helpers ────────────────────────────────────────────────────────────

function matchesFilter(node: GraphNode, filter: GraphFilter): boolean {
  if (filter.agent) {
    const wanted = Array.isArray(filter.agent) ? filter.agent : [filter.agent];
    if (!wanted.includes(node.agent)) return false;
  }
  if (filter.type) {
    const wanted = Array.isArray(filter.type) ? filter.type : [filter.type];
    if (!wanted.includes(node.type)) return false;
  }
  if (filter.tag) {
    if (!node.tags.includes(filter.tag)) return false;
  }
  return true;
}

function hasAnyFilter(f: GraphFilter): boolean {
  return Object.keys(f).some((k) => f[k as keyof GraphFilter] !== undefined);
}

// ── Sub-components ────────────────────────────────────────────────────────────

function PageSkeleton() {
  // h-full so the spinner centres within whatever the parent gives us —
  // standalone /memory/graph uses 100dvh, embedded mode (tab inside
  // VaultMemoryPage) uses the tab pane height. h-[calc(100dvh-4rem)] was
  // larger than the embedded pane, so the centre landed too far down.
  return (
    <div className="flex h-full min-h-[400px] w-full items-center justify-center">
      <div className="flex flex-col items-center gap-3">
        <div className="w-12 h-12 rounded-full border-2 border-[var(--color-accent)] border-t-transparent animate-spin" />
        <p className="text-sm font-mono uppercase tracking-widest text-[var(--color-text-muted)]">
          Building graph…
        </p>
      </div>
    </div>
  );
}

function ErrorState({ error }: { error: Error | null }) {
  return (
    <div className="flex h-full min-h-[400px] w-full items-center justify-center">
      <div
        className="max-w-sm p-6 rounded-xl space-y-2"
        style={{
          background: "rgba(239, 68, 68, 0.05)",
          border: "1px solid rgba(239, 68, 68, 0.2)",
        }}
      >
        <p className="text-sm font-semibold text-[var(--color-error)]">
          Failed to load memory graph
        </p>
        <p className="text-xs text-[var(--color-text-muted)] font-mono break-all">
          {error?.message ?? "Unknown error"}
        </p>
      </div>
    </div>
  );
}

function EmptyState() {
  return (
    <div className="flex h-[calc(100dvh-4rem)] w-full items-center justify-center">
      <div className="flex flex-col items-center gap-4 text-center max-w-xs">
        <div
          className="w-16 h-16 rounded-2xl flex items-center justify-center text-2xl"
          style={{ background: C.accentSubtle, border: `1px solid ${C.borderAccent}` }}
        >
          🧠
        </div>
        <div>
          <p className="text-base font-semibold text-[var(--color-text-primary)]">
            No vault notes yet
          </p>
          <p className="mt-1 text-sm text-[var(--color-text-muted)]">
            Agents write notes to{" "}
            <code className="font-mono text-xs bg-white/5 px-1 py-0.5 rounded">~/.mc/vault/</code>{" "}
            — the graph will populate as they work.
          </p>
        </div>
      </div>
    </div>
  );
}

// ── Voice filter badge ────────────────────────────────────────────────────────

function VoiceFilterBadge({
  filter,
  onClear,
}: {
  filter: GraphFilter;
  onClear: () => void;
}) {
  const parts = Object.entries(filter)
    .filter(([, v]) => v !== undefined)
    .map(([k, v]) => `${k}=${Array.isArray(v) ? v.join(",") : v}`);

  return (
    <div
      className="absolute bottom-4 right-4 z-10 flex items-center gap-2 px-3 py-1.5 rounded-lg"
      style={{
        background: C.accentSubtle,
        border: `1px solid ${C.borderAccent}`,
      }}
    >
      <span className="text-xs font-mono uppercase tracking-wider" style={{ color: C.accent }}>
        ⟪ voice: {parts.join(" · ")} ⟫
      </span>
      <button
        onClick={onClear}
        className="text-[var(--color-text-muted)] hover:text-[var(--color-text-primary)] transition-colors leading-none"
        aria-label="Clear voice highlight"
      >
        ×
      </button>
    </div>
  );
}

// ── Main page component ───────────────────────────────────────────────────────

export interface VaultGraphPageProps {
  /**
   * When true, the page renders without its own h1/title bar and adapts
   * to the parent's height (used when embedded inside MemoryPage as a tab).
   * When false (default), renders as a standalone page with a full title.
   */
  embedded?: boolean;
  /**
   * Voice filter from a parent that owns the WS bridge. When provided,
   * this page skips its own VoiceHighlightBridge to avoid double subscription.
   */
  voiceFilter?: GraphFilter | null;
  /** Matches the parent's clearVoiceHighlight from useVoiceHighlight. */
  clearVoiceHighlight?: () => void;
}

export function VaultGraphPage({
  embedded = false,
  voiceFilter: voiceFilterProp,
  clearVoiceHighlight: clearVoiceHighlightProp,
}: VaultGraphPageProps = {}) {
  // ── Filter / toggle state ─────────────────────────────────────────────────
  const [filter, setFilter] = useState<GraphFilter>({});
  const [showHeatmap, setShowHeatmap] = useState(false);
  const [colorMode, setColorMode] = useState<"type" | "community">("community");
  const [searchQuery, setSearchQuery] = useState("");

  // ── Selection / traversal state ───────────────────────────────────────────
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [traversal, setTraversal] = useState<{ from: string; to: string } | null>(null);
  const [traversalEdge, setTraversalEdge] = useState<{ source: string; target: string } | null>(null);

  const graphRef = useRef<MemoryGraph2DRef | null>(null);
  const queryClient = useQueryClient();

  // ── Data ──────────────────────────────────────────────────────────────────
  const { data, isLoading, isError, error } = useVaultGraph({
    cluster: false,
    heatmap: "30d",
  });

  // ── Voice integration ─────────────────────────────────────────────────────
  // Parent-controlled when voiceFilterProp !== undefined (props passed),
  // otherwise self-contained via the local hook. The bridge follows the same
  // ownership so a parent-controlled page doesn't double-subscribe to WS.
  const isVoiceControlled = voiceFilterProp !== undefined;
  const localVoice = useVoiceHighlight();
  const voiceFilter = isVoiceControlled ? voiceFilterProp : localVoice.voiceFilter;
  const onVoiceHighlight = isVoiceControlled
    ? localVoice.onVoiceHighlight // unused but typesafe — kept for the standalone bridge below
    : localVoice.onVoiceHighlight;
  const clearVoiceHighlight = isVoiceControlled
    ? (clearVoiceHighlightProp ?? (() => {}))
    : localVoice.clearVoiceHighlight;

  // Camera zoom when voice filter arrives and matches nodes in the graph
  useEffect(() => {
    if (!voiceFilter || !data || !graphRef.current) return;
    const matching = data.nodes
      .filter((n) => matchesFilter(n, voiceFilter))
      .map((n) => n.id);
    if (matching.length > 0) {
      graphRef.current.zoomToNodes(matching);
    }
  }, [voiceFilter, data]);

  // ── Live updates via WebSocket ────────────────────────────────────────────
  useVaultStream({
    enabled: !isLoading,
    onMessage: (msg) => {
      if (msg.type === "modified" || msg.type === "compacted") {
        // Debounce via staleTime — invalidate so next poll picks up changes
        queryClient.invalidateQueries({ queryKey: ["vault", "graph"] });
      } else if (msg.type === "deleted") {
        // Drop the deleted note from EVERY vault view (graph, list, detail).
        queryClient.invalidateQueries({ queryKey: ["vault"] });
        // If the deleted note is currently selected, clear the side panel.
        if (msg.path === selectedPath) {
          setSelectedPath(null);
        }
      }
    },
  });

  // ── Filter as VISUAL DIM rather than data exclusion ──────────────────────
  //
  // Critical design decision: when a filter is active we keep ALL nodes in the
  // scene and just lower the opacity of non-matching ones. This:
  //   • prevents the force-simulation from collapsing 5 surviving nodes into
  //     a tiny far-away cluster when the user filters narrowly
  //   • preserves spatial context (the operator still sees where filtered notes sit
  //     in the larger constellation)
  //   • avoids layout re-flow each toggle
  //
  // Voice filter takes precedence over manual filter. Search query further
  // narrows by label substring match.
  const activeFilter = voiceFilter ?? filter;

  const matchingNodeIds = useMemo<Set<string> | null>(() => {
    if (!data) return null;
    const filterActive = hasAnyFilter(activeFilter) || Boolean(searchQuery.trim());
    if (!filterActive) return null;

    const q = searchQuery.trim().toLowerCase();
    const ids = new Set<string>();
    for (const n of data.nodes) {
      if (hasAnyFilter(activeFilter) && !matchesFilter(n, activeFilter)) continue;
      if (q && !n.label.toLowerCase().includes(q)) continue;
      ids.add(n.id);
    }
    return ids;
  }, [data, activeFilter, searchQuery]);

  // ── Handlers ─────────────────────────────────────────────────────────────
  const handleNodeClick = useCallback((path: string) => {
    setSelectedPath(path);
    setTraversal(null); // clear any pending traversal
    setTraversalEdge(null);
  }, []);

  const handleWikilinkClick = useCallback(
    (target: string) => {
      if (!selectedPath || !data) return;
      // Resolve target stem → real vault path in current graph
      const targetNode = data.nodes.find(
        (n) => n.label.toLowerCase() === target.toLowerCase()
      );
      if (!targetNode) {
        // Fallback: treat target as direct path
        setSelectedPath(target);
        return;
      }
      setTraversal({ from: selectedPath, to: targetNode.id });
    },
    [selectedPath, data]
  );

  const handleTraversalComplete = useCallback(() => {
    if (traversal) {
      setSelectedPath(traversal.to);
    }
    setTraversal(null);
    setTraversalEdge(null);
  }, [traversal]);

  // ── Render ────────────────────────────────────────────────────────────────
  if (isLoading) return <PageSkeleton />;
  if (isError) return <ErrorState error={error as Error | null} />;
  if (!data || data.nodes.length === 0) return <EmptyState />;

  const agents = Array.from(new Set(data.nodes.map((n) => n.agent))).sort();

  // Layout — when embedded inside a tab we adapt to parent height and skip
  // the in-canvas title (the tab header already shows context).
  const containerClass = embedded
    ? "relative flex h-full w-full overflow-hidden rounded-2xl"
    : "relative flex h-[calc(100dvh-4rem)] w-full overflow-hidden rounded-2xl";

  // Glass-panel surface — translucent dark with a subtle violet halo at top.
  // Operator feedback: solid var(--color-bg-base) was "extremely dark and
  // hurts the eye". Now the AppShell deep tone shows through, the panel reads as
  // a lit surface instead of a black brick.
  const containerStyle: React.CSSProperties = {
    background: "rgba(12,12,16,0.55)",
    backdropFilter: "blur(20px) saturate(140%)",
    WebkitBackdropFilter: "blur(20px) saturate(140%)",
    border: "1px solid rgba(255,255,255,0.06)",
    boxShadow:
      "inset 0 1px 0 rgba(255,255,255,0.04), 0 30px 60px -30px rgba(0,0,0,0.5)",
  };

  return (
    <div className={containerClass} style={containerStyle}>
      {/* Title bar — only when standalone */}
      {!embedded && (
        <header className="absolute top-0 inset-x-0 z-10 px-6 py-4 pointer-events-none">
          <h1
            className="text-2xl font-bold tracking-tight"
            style={{ color: "var(--color-text-primary)" }}
          >
            memory / graph
          </h1>
          <p
            className="mt-1 text-xs font-mono uppercase tracking-wider"
            style={{ color: "var(--color-text-muted)" }}
          >
            {data.stats.nodes} nodes · {data.stats.edges} edges · {data.stats.clusters} clusters ·{" "}
            built in {data.stats.build_ms}ms
          </p>
        </header>
      )}

      {/* Left filter rail */}
      <GraphFilterSidebar
        filter={filter}
        onFilterChange={setFilter}
        showHeatmap={showHeatmap}
        showClusters={false}
        onHeatmapToggle={setShowHeatmap}
        onClustersToggle={() => {}}
        agents={agents}
        searchQuery={searchQuery}
        onSearchChange={setSearchQuery}
        className="z-10"
      />

      {/* Graph canvas — touch-action:none prevents page-scroll hijacking the
          graph's pan/zoom on iOS (M13). react-force-graph-2d handles touch
          events natively; DPR capping to 2 is handled by the library itself
          (it reads window.devicePixelRatio but clamps canvas dimensions). */}
      <div className="relative flex-1 overflow-hidden" style={{ touchAction: "none" }}>
        {/* Color mode toggle — top-right corner */}
        <div className="absolute top-3 right-3 z-10">
          <select
            value={colorMode}
            onChange={(e) => setColorMode(e.target.value as "type" | "community")}
            className="text-xs font-mono uppercase bg-transparent border border-white/10 px-2 py-1 rounded"
            style={{
              background: "rgba(10,10,10,0.85)",
              color: "var(--color-text-secondary)",
              backdropFilter: "blur(6px)",
            }}
            aria-label="Node color mode"
          >
            <option value="community">By community</option>
            <option value="type">By type</option>
          </select>
        </div>

        <MemoryGraph2D
          ref={graphRef}
          data={data}
          matchingNodeIds={matchingNodeIds}
          selectedPath={selectedPath}
          onNodeClick={handleNodeClick}
          showHeatmap={showHeatmap}
          traversalEdge={traversalEdge}
          colorMode={colorMode}
        />
      </div>

      {/* Right side-panel */}
      <NoteSidePanel
        path={selectedPath}
        onClose={() => setSelectedPath(null)}
        onWikilinkClick={handleWikilinkClick}
      />

      {/* Voice highlight badge */}
      {voiceFilter && (
        <VoiceFilterBadge filter={voiceFilter} onClear={clearVoiceHighlight} />
      )}

      {/* Headless coordinators — bridge skipped when parent owns voice state
          (prevents double WS subscribe). */}
      {!isVoiceControlled && (
        <VoiceHighlightBridge onHighlight={onVoiceHighlight} />
      )}
      {traversal && (
        <TraversalAnimation
          fromPath={traversal.from}
          toPath={traversal.to}
          graphRef={graphRef}
          onTraversalEdge={setTraversalEdge}
          onComplete={handleTraversalComplete}
        />
      )}
    </div>
  );
}
