/**
 * MemoryGraph2D — Obsidian-style Vault Graph (M.4 T6 + T7 + T8, 2D pivot)
 * W5: Louvain communities + Astorian force-settings + hover-fade
 *
 * 2D force-directed graph using react-force-graph-2d (HTML5 Canvas).
 * Replaces the prior MemoryGraph3D — the operator explicitly compared to Obsidian's
 * Graph View, which is 2D. The pivot kills the Three.js camera dance,
 * makes cluster hulls trivially correct, and renders crisply on mobile.
 *
 * SSR LIMITATION: react-force-graph-2d requires browser globals (Canvas,
 * requestAnimationFrame). Import via Next.js `dynamic` with `ssr: false`:
 *
 *   const MemoryGraph2D = dynamic(
 *     () => import("@/components/memory/MemoryGraph2D").then(m => m.MemoryGraph2D),
 *     { ssr: false }
 *   );
 *
 * Visual contract:
 *  - colorMode="type"      → Type-coloured discs (TYPE_COLORS, default)
 *  - colorMode="community" → Louvain community palette (COMMUNITY_PALETTE)
 *  - Node radius via sqrt on link count (nodeRadiusFromLinkCount, 3-12px)
 *    — hubs visibly larger than leaves, like Obsidian's Graph View
 *  - Selected node rendered in BRAND_PURPLE with a thin ring
 *  - Filter-non-match → globalAlpha = NODE_DIMMED_OPACITY + edges fade too
 *  - Hover → neighbour highlight, non-neighbours fade to 0.12 alpha
 *  - Heatmap mode → translucent halo whose radius grows with viewCount
 *  - Traversal edge → BRAND_PURPLE, 2× width during wikilink fly-to
 *  - Forces: charge=-300, linkDistance=25, forceX/Y(0).strength(0.4) for
 *    spherical centering — keeps brain at world origin so framing is stable.
 */

"use client";

import {
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useMemo,
  useRef,
  useState,
} from "react";
import ForceGraph2D from "react-force-graph-2d";
import type { ForceGraphMethods } from "react-force-graph-2d";
import { forceX, forceY } from "d3-force";

import type { GraphNode, VaultGraphResponse } from "@/lib/types";
import {
  BRAND_PURPLE,
  EDGE_COLOR_DEFAULT,
  EDGE_COLOR_HOVER,
  EDGE_WIDTH,
  NODE_DIMMED_OPACITY,
  RESET_DURATION_MS,
  TYPE_COLORS,
  ZOOM_FIT_PADDING_PX,
  colorForCommunity,
  nodeRadiusFromLinkCount,
} from "./graphConfig";
import { computeCommunities } from "@/lib/graphLouvain";

// ── Public imperative handle ──────────────────────────────────────────────────

/**
 * Ref handle exposed to parents.
 * T9 (voice highlight) calls zoomToNodes(ids) when a filter fires.
 * graph2ScreenCoords kept for API compatibility (ClusterOverlay removed in W5).
 */
export interface MemoryGraph2DRef {
  /** Pan + zoom to the centroid of the given node ids (~1.2s). */
  zoomToNodes: (nodeIds: string[]) => void;
  /** Zoom-to-fit reset to the default overview (~0.8s). */
  resetCamera: () => void;
  /**
   * Project a graph world coordinate to screen pixel space.
   * Delegates to react-force-graph-2d's built-in projection helper.
   * Returns {x: 0, y: 0} if the graph is not yet mounted.
   */
  graph2ScreenCoords: (x: number, y: number) => { x: number; y: number };
}

// ── Props ─────────────────────────────────────────────────────────────────────

export interface MemoryGraph2DProps {
  /** Full graph payload from GET /api/v1/vault/graph */
  data: VaultGraphResponse;
  /** Currently selected vault path — rendered in brand purple with a ring */
  selectedPath?: string | null;
  /** Called with the vault path when a node is clicked */
  onNodeClick: (path: string) => void;
  /** Called with vault path on hover, null on leave */
  onNodeHover?: (path: string | null) => void;
  /** When true, draw an activity-halo around nodes with viewCount > 0 */
  showHeatmap?: boolean;
  /**
   * Set of node ids that PASS the active filter. When non-null, nodes NOT in
   * this set are visually de-emphasised (low opacity) instead of removed. This
   * keeps the force-simulation stable when users toggle filters — the layout
   * doesn't reflow chaotically.
   *
   * null = no filter active, all nodes at full opacity.
   */
  matchingNodeIds?: Set<string> | null;
  /**
   * T9 traversal edge — when set, this specific edge is rendered with
   * brand-purple glow (2× width, full opacity) for 800ms during wikilink
   * traversal. TraversalAnimation sets it, then clears after zoom completes.
   */
  traversalEdge?: { source: string; target: string } | null;
  /**
   * W5: Color mode toggle.
   * "type"      → node color from TYPE_COLORS (default legacy behaviour)
   * "community" → Louvain community palette (default)
   */
  colorMode?: "type" | "community";
  className?: string;
}

// ── Internal graph data shape ─────────────────────────────────────────────────

interface LinkDatum {
  source: string;
  target: string;
  weight: number;
}

// ── Component ─────────────────────────────────────────────────────────────────

export const MemoryGraph2D = forwardRef<MemoryGraph2DRef, MemoryGraph2DProps>(
  function MemoryGraph2D(
    {
      data,
      selectedPath,
      onNodeClick,
      onNodeHover,
      showHeatmap,
      traversalEdge,
      matchingNodeIds,
      colorMode = "type",
    },
    ref,
  ) {
    // M13: Cap devicePixelRatio to 2 so the canvas never exceeds iOS's 16M px
    // limit. force-graph reads window.devicePixelRatio directly on each render;
    // we shadow it once on mount and restore on unmount to avoid affecting other
    // canvas consumers. Only applied when DPR > 2 (i.e. 3× iPhone Pro / iPad).
    useEffect(() => {
      const realDPR = window.devicePixelRatio;
      if (realDPR <= 2) return;
      Object.defineProperty(window, "devicePixelRatio", {
        value: 2,
        configurable: true,
        writable: true,
      });
      return () => {
        Object.defineProperty(window, "devicePixelRatio", {
          value: realDPR,
          configurable: true,
          writable: true,
        });
      };
    }, []);

    const fgRef = useRef<ForceGraphMethods | undefined>(undefined);

    // ── Hover-highlight state ─────────────────────────────────────────────────

    const [hoveredId, setHoveredId] = useState<string | null>(null);

    /** Set of hovered node + its direct neighbours (for alpha fade on non-members). */
    const neighbourIds = useMemo(() => {
      if (!hoveredId) return null;
      const ids = new Set<string>([hoveredId]);
      for (const e of data.edges) {
        if (e.source === hoveredId) ids.add(e.target);
        if (e.target === hoveredId) ids.add(e.source);
      }
      return ids;
    }, [hoveredId, data.edges]);

    // ── Louvain community detection (computed once per data) ──────────────────

    const communities = useMemo(
      () =>
        computeCommunities(
          data.nodes.map((n) => ({ id: n.id })),
          data.edges.map((e) => ({ source: e.source, target: e.target, weight: e.weight })),
        ),
      [data],
    );

    // ── Sqrt node sizing from link count ──────────────────────────────────────

    const linkCounts = useMemo(() => {
      const counts: Record<string, number> = {};
      for (const n of data.nodes) counts[n.id] = 0;
      for (const e of data.edges) {
        counts[e.source] = (counts[e.source] ?? 0) + 1;
        counts[e.target] = (counts[e.target] ?? 0) + 1;
      }
      return counts;
    }, [data]);

    // ── Graph data (shape react-force-graph-2d expects) ──────────────────────
    // Memoised so that polling refetches (every 30s) don't produce a new
    // object reference when the graph topology hasn't changed. A new reference
    // causes ForceGraph2D to re-ingest data and restart the simulation, making
    // the layout spring around after each poll. We key only on node IDs +
    // edge count so that metadata-only updates (e.g. viewCount) don't reheat.

    const graphData = useMemo(
      () => ({
        nodes: data.nodes as unknown as object[],
        links: data.edges.map<LinkDatum>((e) => ({
          source: e.source,
          target: e.target,
          weight: e.weight,
        })) as unknown as object[],
      }),
      // eslint-disable-next-line react-hooks/exhaustive-deps
      [
        data.nodes.length,
        data.edges.length,
        // Join of ids is cheap for ~317 nodes and catches node add/remove
        // without reacting to per-node metadata changes (viewCount, etc.).
        // eslint-disable-next-line react-hooks/exhaustive-deps
        data.nodes.map((n) => n.id).join(","),
      ],
    );

    // Force overrides matching Obsidian's Graph View characteristics:
    //   - charge -100: moderate repulsion → leaves spread around hubs
    //   - linkDistance 30: SHORT → leaves cluster tight around their hub
    //   - centerStrength 0.5: STRONG centering → graph stays in a sphere
    //
    // Combined with link-count-based node sizing (hubs visibly larger), this
    // produces the radial constellation look from the operator's reference Obsidian
    // screenshot — distinct hubs, leaves orbit, tight central form.

    const [forcesInitialized, setForcesInitialized] = useState(false);

    useEffect(() => {
      if (!fgRef.current || forcesInitialized) return;
      const fg = fgRef.current as unknown as {
        d3Force: (name: string, force?: unknown) => unknown;
        d3ReheatSimulation: () => void;
      };
      // Charge: -300 for clear repulsion at 317 nodes / 1350 edges.
      const chargeForce = fg.d3Force("charge") as
        | { strength: (v: number) => unknown }
        | undefined;
      chargeForce?.strength(-300);
      // Link distance: short, Obsidian-style — leaves cluster tight on hubs.
      const linkForce = fg.d3Force("link") as
        | { distance: (v: number) => unknown }
        | undefined;
      linkForce?.distance(25);
      // forceX/forceY pull every node toward world origin (0,0). 0.4 is
      // the highest setting that keeps the constellation shape — beyond
      // ~0.5 the brain crushes into a tight ball. Strong enough that
      // bbox stays symmetric around (0,0), so the post-fit centerAt
      // doesn't have to compensate for outlier drift.
      fg.d3Force("x", forceX(0).strength(0.4));
      fg.d3Force("y", forceY(0).strength(0.4));
      fg.d3ReheatSimulation();
      setForcesInitialized(true);
    }, [forcesInitialized]);

    // Auto-fit at multiple checkpoints so the brain is reliably framed
    // regardless of how/when the simulation settles or how the canvas is
    // resized. didFitOnce guard prevents subsequent reheats (filter toggles,
    // drags) from yanking the camera away from the user's manual pan/zoom.
    const didFitOnceRef = useRef(false);

    const fitNow = useCallback(() => {
      if (didFitOnceRef.current || !fgRef.current) return;
      const fg = fgRef.current as unknown as {
        zoomToFit: (ms: number, padding: number) => void;
        centerAt: (x: number, y: number, ms?: number) => void;
      };
      // Two-step framing:
      //   1. zoomToFit picks the correct zoom level so all nodes fit.
      //      Padding 220 = the operator wanted "~25% mehr zoom out" room around
      //      the bbox vs the previous 140.
      //   2. centerAt(0, +Y) re-centres camera slightly below origin
      //      AFTER zoom completes. Positive Y in world coords pulls the
      //      brain upward in the viewport — the operator wanted it "ein bischen
      //      nach oben". forceX/Y already keeps the dense mass at (0,0)
      //      so this offset is purely framing.
      // On a narrow (mobile) canvas a 220px padding is wider than the canvas
      // itself, so the bbox collapses to a dot. Scale padding + vertical
      // framing offset down for small viewports.
      const isMobileViewport =
        typeof window !== "undefined" && window.innerWidth < 768;
      fg.zoomToFit(500, isMobileViewport ? 40 : 220);
      setTimeout(() => {
        if (fgRef.current) {
          (fgRef.current as unknown as {
            centerAt: (x: number, y: number, ms?: number) => void;
          }).centerAt(0, isMobileViewport ? 40 : 150, 500);
        }
      }, 520);
      didFitOnceRef.current = true;
    }, []);

    const handleEngineStop = useCallback(() => {
      fitNow();
    }, [fitNow]);

    // Two timed fallbacks:
    //   - 1500ms: catches the case where engine stops fast but
    //     onEngineStop didn't fire (some library versions skip it).
    //   - 4000ms: catches forceX/Y energy injection that prevents alpha
    //     from ever hitting min.
    // Both go through fitNow() which is idempotent via didFitOnceRef.
    useEffect(() => {
      const t1 = setTimeout(() => fitNow(), 1500);
      const t2 = setTimeout(() => fitNow(), 4000);
      return () => {
        clearTimeout(t1);
        clearTimeout(t2);
      };
    }, [fitNow]);

    // ── Custom canvas renderer ────────────────────────────────────────────────
    //
    // Node color priority:
    //   1. Selected → BRAND_PURPLE (always)
    //   2. colorMode="community" → community palette
    //   3. colorMode="type"      → type color
    //
    // Alpha priority (combined):
    //   - filter dim (matchingNodeIds) → NODE_DIMMED_OPACITY
    //   - hover fade (neighbourIds)    → 0.12 for non-neighbours
    //   - full opacity otherwise

    const LABEL_ZOOM_THRESHOLD = 2.2;

    const drawNode = useCallback(
      (node: object, ctx: CanvasRenderingContext2D, globalScale: number) => {
        const n = node as GraphNode & { x?: number; y?: number };
        const x = n.x ?? 0;
        const y = n.y ?? 0;
        const radius = nodeRadiusFromLinkCount(linkCounts[n.id] ?? 0);
        const isSelected = selectedPath === n.id;

        // Color resolution
        let baseColor: string;
        if (isSelected) {
          baseColor = BRAND_PURPLE;
        } else if (colorMode === "community") {
          baseColor = colorForCommunity(communities[n.id] ?? 0);
        } else {
          baseColor = TYPE_COLORS[n.type] ?? TYPE_COLORS.note;
        }

        // Alpha resolution — most restrictive wins
        const isDimmed =
          matchingNodeIds !== null && matchingNodeIds !== undefined && !matchingNodeIds.has(n.id);
        const isHoverFaded = neighbourIds !== null && !neighbourIds.has(n.id);
        const alpha = isDimmed || isHoverFaded ? NODE_DIMMED_OPACITY : 1;

        ctx.save();
        ctx.globalAlpha = alpha;

        // Heatmap halo — only nodes with actual view activity
        if (showHeatmap && (n.viewCount ?? 0) > 0) {
          const haloRadius = radius + Math.log2(n.viewCount + 1) * 1.6;
          ctx.beginPath();
          ctx.arc(x, y, haloRadius, 0, 2 * Math.PI);
          ctx.fillStyle = baseColor;
          ctx.globalAlpha = alpha * 0.18;
          ctx.fill();
          ctx.globalAlpha = alpha;
        }

        // Core disc
        ctx.beginPath();
        ctx.arc(x, y, radius, 0, 2 * Math.PI);
        ctx.fillStyle = baseColor;
        ctx.fill();

        // Selected ring (drawn after fill so it sits on top)
        if (isSelected) {
          ctx.beginPath();
          ctx.arc(x, y, radius + 2.5, 0, 2 * Math.PI);
          ctx.strokeStyle = "#F5F5F5";
          ctx.lineWidth = 1.5 / globalScale;
          ctx.stroke();
        }

        // Label — only at high zoom (avoid overdraw at overview scale)
        if (globalScale >= LABEL_ZOOM_THRESHOLD) {
          const fontSize = 11 / globalScale;
          ctx.font = `${fontSize}px 'Geist Mono', monospace`;
          ctx.textAlign = "center";
          ctx.textBaseline = "top";
          ctx.fillStyle = "#F5F5F5";
          ctx.fillText(n.label, x, y + radius + 2 / globalScale);
        }

        ctx.restore();
      },
      [selectedPath, matchingNodeIds, showHeatmap, colorMode, communities, neighbourIds, linkCounts],
    );

    // Pointer hit-area = the visible disc (matches drawNode sizing).
    const nodePointerAreaPaint = useCallback(
      (node: object, color: string, ctx: CanvasRenderingContext2D) => {
        const n = node as GraphNode & { x?: number; y?: number };
        const radius = nodeRadiusFromLinkCount(linkCounts[n.id] ?? 0);
        ctx.beginPath();
        ctx.arc(n.x ?? 0, n.y ?? 0, radius + 1, 0, 2 * Math.PI);
        ctx.fillStyle = color;
        ctx.fill();
      },
      [linkCounts],
    );

    const nodeLabel = useCallback((node: object) => {
      const n = node as GraphNode;
      // HTML tooltip emitted by the library on hover.
      return `<div style="font-family:'Geist Mono',monospace;font-size:11px;color:#F5F5F5;background:rgba(10,10,10,0.92);padding:4px 8px;border-radius:4px;line-height:1.4;pointer-events:none;">${n.label}<br/><span style="opacity:0.55">${n.type} · ${n.agent}</span></div>`;
    }, []);

    // ── Link color — filter-aware + hover-fade + traversal highlight ──────────

    const linkColor = useCallback(
      (link: object) => {
        const l = link as { source: { id?: string } | string; target: { id?: string } | string };
        const srcId = typeof l.source === "object" ? l.source.id : l.source;
        const tgtId = typeof l.target === "object" ? l.target.id : l.target;

        // Traversal highlight takes priority
        if (traversalEdge && srcId === traversalEdge.source && tgtId === traversalEdge.target) {
          return BRAND_PURPLE;
        }

        // Filter dim — when a filter is active, only edges that connect two
        // matching nodes stay visible. Otherwise the canvas turns into a
        // spaghetti mess of edges pointing to invisible (dimmed) nodes.
        if (matchingNodeIds) {
          const bothMatch =
            srcId !== undefined && matchingNodeIds.has(srcId) &&
            tgtId !== undefined && matchingNodeIds.has(tgtId);
          if (!bothMatch) return "rgba(255,255,255,0.025)";
        }

        // Hover neighbourhood fade
        if (neighbourIds) {
          const involved =
            (srcId !== undefined && neighbourIds.has(srcId)) &&
            (tgtId !== undefined && neighbourIds.has(tgtId));
          return involved ? EDGE_COLOR_HOVER : "rgba(255,255,255,0.04)";
        }

        return EDGE_COLOR_DEFAULT;
      },
      [traversalEdge, neighbourIds, matchingNodeIds],
    );

    const linkWidth = useCallback(
      (link: object) => {
        const l = link as { source: { id?: string } | string; target: { id?: string } | string };
        const srcId = typeof l.source === "object" ? l.source.id : l.source;
        const tgtId = typeof l.target === "object" ? l.target.id : l.target;
        if (traversalEdge && srcId === traversalEdge.source && tgtId === traversalEdge.target) {
          return 2;
        }
        return EDGE_WIDTH;
      },
      [traversalEdge],
    );

    // ── Interaction callbacks ─────────────────────────────────────────────────

    const handleNodeClick = useCallback(
      (node: object) => {
        onNodeClick((node as GraphNode).id);
      },
      [onNodeClick],
    );

    const handleNodeHover = useCallback(
      (node: object | null) => {
        const id = node ? (node as GraphNode).id : null;
        setHoveredId(id);
        onNodeHover?.(id);
      },
      [onNodeHover],
    );

    // ── Imperative handle for T9 voice-highlight ──────────────────────────────

    useImperativeHandle(
      ref,
      () => ({
        zoomToNodes(ids: string[]) {
          // Operator feedback iteration (2026-05-15):
          //
          // v1 — centerAt(centroid) + zoom(level=3): too cropped, lost context.
          // v2 — zoomToFit(): re-centred on the bounding-box midpoint, which
          //      drifted away from the visual "centre of brain" because
          //      d3-force keeps mass-centre at (0,0) but outliers stretch the
          //      bounding box asymmetrically.
          // v3 — DON'T move the camera at all. The visual highlight already
          //      comes from matchingNodeIds dimming non-matches to 12% alpha.
          //      The operator's mental model is "the brain stays in the middle, the
          //      voice command just lights up a subset". Cargo-keep the ids
          //      param so the method signature on MemoryGraph2DRef stays
          //      compatible with VaultGraphPage's existing call site.
          void ids;
          return;
        },

        resetCamera() {
          if (!fgRef.current) return;
          // zoomToFit fits all nodes within the canvas with padding.
          fgRef.current.zoomToFit(RESET_DURATION_MS, ZOOM_FIT_PADDING_PX);
        },

        // Kept for API compatibility — ClusterOverlay was removed in W5.
        graph2ScreenCoords(x: number, y: number) {
          if (!fgRef.current) return { x: 0, y: 0 };
          return fgRef.current.graph2ScreenCoords(x, y);
        },
      }),
      [data.nodes],
    );

    // ── Render ────────────────────────────────────────────────────────────────
    //
    // enableNodeDrag={true} = Obsidian's signature "rubber band" feel:
    // click a node, drag it, connected nodes follow via the link force, and
    // when you release, the constellation springs back. ForceGraph2D handles
    // the alphaTarget bookkeeping internally (warms sim on drag-start, cools
    // on drag-end), so we don't need any custom mousemove logic.

    return (
      <ForceGraph2D
        ref={fgRef}
        graphData={graphData}
        // ── Presentation ──────────────────────────────────────────────────────
        backgroundColor="rgba(0,0,0,0)"
        nodeRelSize={1} // ignored — we draw our own discs via nodeCanvasObject
        nodeLabel={nodeLabel}
        nodeCanvasObject={drawNode}
        nodeCanvasObjectMode={() => "replace"}
        nodePointerAreaPaint={nodePointerAreaPaint}
        // ── Edges ─────────────────────────────────────────────────────────────
        linkColor={linkColor}
        linkWidth={linkWidth}
        linkDirectionalParticles={0}
        // ── Interaction ───────────────────────────────────────────────────────
        onNodeClick={handleNodeClick}
        onNodeHover={handleNodeHover}
        enableNodeDrag={true}
        enablePanInteraction={true}
        enableZoomInteraction={true}
        // Predictable sim termination so onEngineStop fires within ~2.5s.
        // Library defaults are cooldownTicks=Infinity + cooldownTime=15000,
        // which delays the auto-fit too long (the operator gave up waiting).
        cooldownTicks={120}
        cooldownTime={2500}
        onEngineStop={handleEngineStop}
      />
    );
  },
);

MemoryGraph2D.displayName = "MemoryGraph2D";
