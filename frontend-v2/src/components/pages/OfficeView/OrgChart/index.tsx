"use client";

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { motion, useReducedMotion } from "framer-motion";
import { OrgChartNode } from "./OrgChartNode";
import { ORG_CHART, getChildren, getRoot } from "./org-chart-data";
import type { OrgNode } from "./types";
import { C, STATUS } from "@/lib/colors";

/**
 * Connector descriptor — computed from card DOM rects relative to the
 * wrapping <svg>. Two flavours:
 *   - "solid"  → standard parent→child line
 *   - "voice"  → dashed cyan line for the operator→Jarvis sideways branch
 */
interface Connector {
  id: string;
  d: string;
  variant: "solid" | "voice";
}

// ── Stagger schedule ──────────────────────────────────────────────────────

function tierDelay(node: OrgNode, index: number): number {
  switch (node.tier) {
    case "operator": return 0;
    case "voice":    return 0.18;
    case "lead":     return 0.22;
    case "worker":   return 0.42 + index * 0.045;
  }
}

// ── Connector builder ─────────────────────────────────────────────────────

function buildPath(
  fromRect: DOMRect,
  toRect: DOMRect,
  svgRect: DOMRect,
  variant: Connector["variant"]
): string {
  const fromX = fromRect.left + fromRect.width / 2 - svgRect.left;
  const fromY = fromRect.bottom - svgRect.top;
  const toX   = toRect.left + toRect.width / 2 - svgRect.left;
  const toY   = toRect.top - svgRect.top;

  if (variant === "voice") {
    // Sideways curve — leaves the operator from the left side, arcs into Jarvis.
    // Approximation: we still go bottom→top but with a wider horizontal
    // sweep so it reads as "different branch".
    const midY = (fromY + toY) / 2;
    return `M ${fromX} ${fromY} C ${fromX} ${midY + 24}, ${toX} ${midY - 24}, ${toX} ${toY}`;
  }

  const midY = (fromY + toY) / 2;
  return `M ${fromX} ${fromY} C ${fromX} ${midY}, ${toX} ${midY}, ${toX} ${toY}`;
}

// ── Mobile detection (graceful collapse) ─────────────────────────────────

function useIsCompact() {
  const [compact, setCompact] = useState(false);
  useEffect(() => {
    const mql = window.matchMedia("(max-width: 640px)");
    const handler = () => setCompact(mql.matches);
    handler();
    mql.addEventListener("change", handler);
    return () => mql.removeEventListener("change", handler);
  }, []);
  return compact;
}

// ── Main ──────────────────────────────────────────────────────────────────

interface OrgChartProps {
  /** Forces connector path recompute when the parent's zoom level changes.
   *  CSS `zoom` reflows layout but ResizeObserver doesn't always fire on
   *  every nested element fast enough — passing zoom as a dep guarantees
   *  one recompute per zoom-level change. */
  zoom?: number;
}

export function OrgChart({ zoom = 1 }: OrgChartProps = {}) {
  const reduceMotion = useReducedMotion();
  const isCompact = useIsCompact();
  const data = ORG_CHART;
  const root = getRoot(data);

  const containerRef = useRef<HTMLDivElement>(null);
  const svgRef       = useRef<SVGSVGElement>(null);
  const nodeRefs     = useRef<Record<string, HTMLDivElement | null>>({});
  const [connectors, setConnectors] = useState<Connector[]>([]);

  // Recompute connector paths after layout (and on resize).
  useLayoutEffect(() => {
    if (isCompact) {
      setConnectors([]);
      return;
    }

    const compute = () => {
      const svgEl = svgRef.current;
      if (!svgEl) return;
      const svgRect = svgEl.getBoundingClientRect();

      const next: Connector[] = [];
      for (const node of data.nodes) {
        if (!node.parentId) continue;
        const parentEl = nodeRefs.current[node.parentId];
        const childEl  = nodeRefs.current[node.id];
        if (!parentEl || !childEl) continue;
        const fromRect = parentEl.getBoundingClientRect();
        const toRect   = childEl.getBoundingClientRect();
        const variant: Connector["variant"] =
          node.id === "jarvis" ? "voice" : "solid";
        next.push({
          id: `${node.parentId}->${node.id}`,
          d: buildPath(fromRect, toRect, svgRect, variant),
          variant,
        });
      }
      setConnectors(next);
    };

    compute();

    const ro = new ResizeObserver(compute);
    if (containerRef.current) ro.observe(containerRef.current);
    Object.values(nodeRefs.current).forEach((el) => el && ro.observe(el));
    window.addEventListener("resize", compute);

    // Recompute once after fonts/transitions settle.
    const t = window.setTimeout(compute, 350);

    return () => {
      ro.disconnect();
      window.removeEventListener("resize", compute);
      window.clearTimeout(t);
    };
  }, [data.nodes, isCompact, zoom]);

  if (!root) return null;

  // ── Compact (mobile) layout: vertical stack, no connectors ──────────────
  if (isCompact) {
    return (
      <div className="px-4 py-6 space-y-3 min-h-[100dvh]">
        {data.nodes.map((node, i) => (
          <motion.div
            key={node.id}
            initial={reduceMotion ? false : { opacity: 0, y: 12 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{
              type: "spring",
              stiffness: 220,
              damping: 24,
              delay: reduceMotion ? 0 : i * 0.04,
            }}
            className="flex justify-center"
            ref={(el) => { nodeRefs.current[node.id] = el; }}
          >
            <OrgChartNode node={node} />
          </motion.div>
        ))}
      </div>
    );
  }

  const jarvis = data.nodes.find((n) => n.id === "jarvis");
  const boss   = data.nodes.find((n) => n.id === "boss");
  const workers = boss ? getChildren(boss.id, data) : [];

  return (
    <div
      ref={containerRef}
      className="relative mx-auto w-full max-w-[1280px] px-6 pt-12 pb-16"
      style={{ minHeight: "calc(100dvh - 80px)" }}
    >
      {/* Connector SVG — sits behind cards, covers the whole tree */}
      <svg
        ref={svgRef}
        aria-hidden
        className="pointer-events-none absolute inset-0 w-full h-full"
        style={{ zIndex: 0 }}
      >
        <defs>
          {/* Operator feedback: lines were nearly invisible on macOS. Bumped
              opacity from 0.16→0.45 and thickness from 1.2→1.8 so they
              read on the glass panel against AppShell deep tone. */}
          <linearGradient id="org-line-solid" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%"   stopColor="rgba(255,255,255,0.45)" />
            <stop offset="100%" stopColor="rgba(255,255,255,0.18)" />
          </linearGradient>
          <linearGradient id="org-line-voice" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%"   stopColor={`${C.accent}D9`} />
            <stop offset="100%" stopColor={`${C.accent}59`} />
          </linearGradient>
        </defs>
        {connectors.map((c, i) => (
          <motion.path
            key={c.id}
            d={c.d}
            fill="none"
            strokeWidth={c.variant === "voice" ? 2 : 1.8}
            strokeDasharray={c.variant === "voice" ? "5 4" : undefined}
            stroke={
              c.variant === "voice"
                ? "url(#org-line-voice)"
                : "url(#org-line-solid)"
            }
            initial={reduceMotion ? false : { pathLength: 0, opacity: 0 }}
            animate={{ pathLength: 1, opacity: 1 }}
            transition={{
              pathLength: { duration: 0.7, delay: 0.45 + i * 0.04, ease: "easeOut" },
              opacity:    { duration: 0.3, delay: 0.45 + i * 0.04 },
            }}
          />
        ))}
      </svg>

      {/* ── Level 1: Operator (root) ──────────────────────────────────── */}
      <div className="relative z-[1] flex justify-center">
        <NodeWrapper
          node={root}
          delay={tierDelay(root, 0)}
          reduceMotion={!!reduceMotion}
          registerRef={(el) => { nodeRefs.current[root.id] = el; }}
        />
      </div>

      {/* ── Level 2: Jarvis (in-line between the operator and Boss) ───── */}
      {jarvis && (
        <div className="relative z-[1] mt-[72px] flex justify-center">
          <NodeWrapper
            node={jarvis}
            delay={tierDelay(jarvis, 0)}
            reduceMotion={!!reduceMotion}
            registerRef={(el) => { nodeRefs.current[jarvis.id] = el; }}
          />
        </div>
      )}

      {/* ── Level 3: Boss (under Jarvis) ──────────────────────────────── */}
      {boss && (
        <div className="relative z-[1] mt-[72px] flex justify-center">
          <NodeWrapper
            node={boss}
            delay={tierDelay(boss, 0)}
            reduceMotion={!!reduceMotion}
            registerRef={(el) => { nodeRefs.current[boss.id] = el; }}
          />
        </div>
      )}

      {/* ── Level 4: Workers (under Boss) ─────────────────────────────── */}
      <div
        className="relative z-[1] mt-[104px] grid gap-x-5 gap-y-6 justify-center"
        style={{
          gridTemplateColumns:
            "repeat(auto-fit, minmax(208px, 208px))",
          maxWidth: 208 * 5 + 5 * 4,
          marginLeft: "auto",
          marginRight: "auto",
        }}
      >
        {workers.map((worker, i) => (
          <NodeWrapper
            key={worker.id}
            node={worker}
            delay={tierDelay(worker, i)}
            reduceMotion={!!reduceMotion}
            registerRef={(el) => { nodeRefs.current[worker.id] = el; }}
          />
        ))}
      </div>

      {/* Legend — small, sits at bottom-left */}
      <Legend />
    </div>
  );
}

// ── Helpers ───────────────────────────────────────────────────────────────

function NodeWrapper({
  node,
  delay,
  reduceMotion,
  registerRef,
}: {
  node: OrgNode;
  delay: number;
  reduceMotion: boolean;
  registerRef: (el: HTMLDivElement | null) => void;
}) {
  return (
    <motion.div
      ref={registerRef}
      initial={reduceMotion ? false : { opacity: 0, y: 14, scale: 0.96 }}
      animate={{ opacity: 1, y: 0, scale: 1 }}
      transition={{
        type: "spring",
        stiffness: 280,
        damping: 26,
        delay: reduceMotion ? 0 : delay,
      }}
    >
      <OrgChartNode node={node} />
    </motion.div>
  );
}

function Legend() {
  return (
    <div
      className="mt-12 mx-auto flex flex-wrap items-center justify-center gap-x-5 gap-y-2 text-[10px] text-zinc-500"
      aria-label="Legend"
    >
      <LegendItem swatch={<LineSwatch variant="solid" />} label="Dispatch-Linie" />
      <LegendItem swatch={<LineSwatch variant="voice"  />} label="Voice-Branch" />
      <span className="hidden sm:inline-block h-3 w-px bg-zinc-800" />
      <LegendItem swatch={<Dot color={C.online} />} label="online" />
      <LegendItem swatch={<Dot color={C.accent} />} label="working" />
      <LegendItem swatch={<Dot color={STATUS.offline} />} label="offline" />
    </div>
  );
}

function LegendItem({ swatch, label }: { swatch: React.ReactNode; label: string }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      {swatch}
      <span className="uppercase tracking-[0.1em]">{label}</span>
    </span>
  );
}

function LineSwatch({ variant }: { variant: "solid" | "voice" }) {
  if (variant === "voice") {
    return (
      <svg width="22" height="6" aria-hidden>
        <line
          x1="0" y1="3" x2="22" y2="3"
          stroke={C.accent}
          strokeWidth="1.4"
          strokeDasharray="4 3"
          opacity="0.7"
        />
      </svg>
    );
  }
  return (
    <svg width="22" height="6" aria-hidden>
      <line x1="0" y1="3" x2="22" y2="3" stroke="rgba(255,255,255,0.3)" strokeWidth="1.2" />
    </svg>
  );
}

function Dot({ color }: { color: string }) {
  return (
    <span
      className="inline-block rounded-full"
      style={{ width: 6, height: 6, background: color }}
    />
  );
}
