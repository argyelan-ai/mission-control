"use client";

/**
 * GraphFilterSidebar — left-rail filter controls for the 3D Memory Graph.
 *
 * 220 px wide, dark off-black, collapsible.
 * Sections:
 *   1. Search — client-side fuzzy filter on node labels
 *   2. TYPE chips — 8 types, multi-select, type-color tint when selected
 *   3. AGENT chips — per-agent multi-select, identity-color dot
 *   4. Toggles — heatmap / cluster overlay
 *   5. Reset — clears all filters
 *
 * All state is lifted to the parent (T10 page) via props. This component is
 * purely presentational — it fires callbacks on every change.
 *
 * Design system:
 *   - Geist Mono for section headers (UPPERCASE, tracking-wider, text-secondary)
 *   - Type colors from graphConfig.TYPE_COLORS at 15% opacity when selected
 *   - Accent-Teal (C.accent) ONLY for the selected ring outline
 *   - Framer Motion spring slide-in/out (stiffness 220, damping 26)
 *   - prefers-reduced-motion: AnimatePresence respects it via CSS media query
 */

import { useState, useCallback, useEffect } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { ChevronLeft, ChevronRight, RotateCcw, Search } from "lucide-react";

import type { GraphFilter, VaultNoteType } from "@/lib/types";
import { TYPE_COLORS, TYPE_LABELS_DE } from "./graphConfig";
import { colorForAgent } from "@/components/vault/agentColors";
import { C } from "@/lib/colors";

// ── Constants ─────────────────────────────────────────────────────────────────

const VAULT_TYPES: VaultNoteType[] = [
  "lesson",
  "knowledge",
  "reference",
  "journal",
  "weekly_review",
  "note",
];

// ── Helpers ───────────────────────────────────────────────────────────────────

function hexToRgb(hex: string): string {
  const h = hex.replace("#", "");
  const r = parseInt(h.slice(0, 2), 16);
  const g = parseInt(h.slice(2, 4), 16);
  const b = parseInt(h.slice(4, 6), 16);
  return `${r},${g},${b}`;
}

// ── Sub-components ────────────────────────────────────────────────────────────

function SectionHeader({ children }: { children: React.ReactNode }) {
  return (
    <div
      className="font-mono uppercase tracking-widest mb-2"
      style={{ fontSize: "9px", color: "var(--color-text-muted)" }}
    >
      {children}
    </div>
  );
}

interface TypeChipProps {
  type: VaultNoteType;
  selected: boolean;
  onToggle: (type: VaultNoteType) => void;
}

function TypeChip({ type, selected, onToggle }: TypeChipProps) {
  const color = TYPE_COLORS[type] ?? TYPE_COLORS.note;
  const rgb = hexToRgb(color);
  const label = TYPE_LABELS_DE[type];

  return (
    <button
      type="button"
      onClick={() => onToggle(type)}
      className="flex items-center gap-1.5 w-full rounded px-2 py-1 text-left transition-colors"
      style={{
        background: selected
          ? `rgba(${rgb},0.15)`
          : "rgba(255,255,255,0.0)",
        border: selected
          ? `1px solid rgba(${rgb},0.4)`
          : "1px solid transparent",
        color: selected
          ? "var(--color-text-primary)"
          : "var(--color-text-secondary)",
        cursor: "pointer",
      }}
      onMouseEnter={(e) => {
        if (!selected) {
          (e.currentTarget as HTMLButtonElement).style.background = `rgba(${rgb},0.07)`;
        }
      }}
      onMouseLeave={(e) => {
        if (!selected) {
          (e.currentTarget as HTMLButtonElement).style.background = "rgba(255,255,255,0.0)";
        }
      }}
      aria-pressed={selected}
      aria-label={`Filter by type: ${label}`}
    >
      <span
        className="inline-block rounded-full shrink-0"
        style={{ width: "6px", height: "6px", background: color }}
      />
      <span className="font-mono text-[11px] truncate">{label}</span>
    </button>
  );
}

interface AgentChipProps {
  agent: string;
  selected: boolean;
  onToggle: (agent: string) => void;
}

function AgentChip({ agent, selected, onToggle }: AgentChipProps) {
  const color = colorForAgent(agent);

  return (
    <button
      type="button"
      onClick={() => onToggle(agent)}
      className="flex items-center gap-1.5 w-full rounded px-2 py-1 text-left transition-colors"
      style={{
        background: selected ? C.accentSubtle : "rgba(255,255,255,0.0)",
        border: selected ? `1px solid ${C.borderAccent}` : "1px solid transparent",
        color: selected ? "var(--color-text-primary)" : "var(--color-text-secondary)",
        cursor: "pointer",
      }}
      onMouseEnter={(e) => {
        if (!selected) {
          (e.currentTarget as HTMLButtonElement).style.background = "rgba(255,255,255,0.04)";
        }
      }}
      onMouseLeave={(e) => {
        if (!selected) {
          (e.currentTarget as HTMLButtonElement).style.background = "rgba(255,255,255,0.0)";
        }
      }}
      aria-pressed={selected}
      aria-label={`Filter by agent: ${agent}`}
    >
      <span
        className="inline-block rounded-full shrink-0"
        style={{ width: "6px", height: "6px", background: color }}
      />
      <span className="font-mono text-[11px] truncate">{agent}</span>
    </button>
  );
}

interface ToggleRowProps {
  label: string;
  enabled: boolean;
  onToggle: (enabled: boolean) => void;
}

function ToggleRow({ label, enabled, onToggle }: ToggleRowProps) {
  return (
    <button
      type="button"
      onClick={() => onToggle(!enabled)}
      className="flex items-center gap-2 w-full rounded px-2 py-1.5 text-left transition-colors"
      style={{
        background: enabled ? C.accentSubtle : "transparent",
        border: enabled ? `1px solid ${C.borderAccent}` : "1px solid transparent",
        color: enabled ? "var(--color-text-primary)" : "var(--color-text-secondary)",
        cursor: "pointer",
      }}
      aria-pressed={enabled}
    >
      <span
        className="inline-block rounded-sm shrink-0"
        style={{
          width: "10px",
          height: "10px",
          border: enabled
            ? `1.5px solid ${C.accent}`
            : "1.5px solid rgba(255,255,255,0.2)",
          background: enabled ? C.accent : "transparent",
        }}
      />
      <span className="font-mono text-[11px]">{label}</span>
    </button>
  );
}

// ── Main component ────────────────────────────────────────────────────────────

export interface GraphFilterSidebarProps {
  filter: GraphFilter;
  onFilterChange: (filter: GraphFilter) => void;
  showHeatmap: boolean;
  showClusters: boolean;
  onHeatmapToggle: (enabled: boolean) => void;
  onClustersToggle: (enabled: boolean) => void;
  /** Available agent slugs derived from graph data. */
  agents: string[];
  /** Optional: controlled search string (lifted to parent for node label filtering). */
  searchQuery?: string;
  onSearchChange?: (q: string) => void;
  className?: string;
}

export function GraphFilterSidebar({
  filter,
  onFilterChange,
  showHeatmap,
  showClusters,
  onHeatmapToggle,
  onClustersToggle,
  agents,
  searchQuery = "",
  onSearchChange,
  className,
}: GraphFilterSidebarProps) {
  const [collapsed, setCollapsed] = useState(false);

  // On mobile the 220px rail overlays nearly the whole canvas (it sits as an
  // absolute overlay), so the graph framed behind it reads as a tiny dot and
  // touch pan/zoom is dead under the panel. Start collapsed below `md` and
  // re-collapse whenever the viewport crosses into mobile. Desktop default
  // (expanded) is untouched. The chevron toggle still opens it as a drawer.
  useEffect(() => {
    const mq = window.matchMedia("(max-width: 767px)");
    const apply = () => {
      if (mq.matches) setCollapsed(true);
    };
    apply();
    mq.addEventListener("change", apply);
    return () => mq.removeEventListener("change", apply);
  }, []);

  // ── Derived selection state ───────────────────────────────────────────────

  const selectedTypes: VaultNoteType[] = Array.isArray(filter.type)
    ? filter.type
    : filter.type
    ? [filter.type]
    : [];

  const selectedAgents: string[] = Array.isArray(filter.agent)
    ? filter.agent
    : filter.agent
    ? [filter.agent]
    : [];

  // ── Callbacks ─────────────────────────────────────────────────────────────

  const toggleType = useCallback(
    (type: VaultNoteType) => {
      const next = selectedTypes.includes(type)
        ? selectedTypes.filter((t) => t !== type)
        : [...selectedTypes, type];
      onFilterChange({ ...filter, type: next.length === 0 ? undefined : next });
    },
    [filter, onFilterChange, selectedTypes],
  );

  const toggleAgent = useCallback(
    (agent: string) => {
      const next = selectedAgents.includes(agent)
        ? selectedAgents.filter((a) => a !== agent)
        : [...selectedAgents, agent];
      onFilterChange({ ...filter, agent: next.length === 0 ? undefined : next });
    },
    [filter, onFilterChange, selectedAgents],
  );

  const handleReset = useCallback(() => {
    onFilterChange({});
    onSearchChange?.("");
    onHeatmapToggle(false);
    onClustersToggle(false);
  }, [onFilterChange, onSearchChange, onHeatmapToggle, onClustersToggle]);

  const hasActiveFilters =
    selectedTypes.length > 0 ||
    selectedAgents.length > 0 ||
    showHeatmap ||
    showClusters ||
    searchQuery.length > 0;

  // ── Render ────────────────────────────────────────────────────────────────

  return (
    // Absolute overlay so the sidebar floats above the canvas without
    // consuming layout space. Graph centroid frames in the full viewport
    // (not just "viewport minus 220px sidebar"). pointer-events-none on
    // the wrapper would block clicks on collapsed-toggle, so we keep auto
    // on the panel + auto on the chevron.
    <div className={`absolute left-0 top-0 bottom-0 flex z-10 ${className ?? ""}`}>
      {/* Sidebar panel */}
      <AnimatePresence initial={false}>
        {!collapsed && (
          <motion.aside
            key="sidebar"
            initial={{ width: 0, opacity: 0 }}
            animate={{ width: 220, opacity: 1 }}
            exit={{ width: 0, opacity: 0 }}
            transition={{ type: "spring", stiffness: 220, damping: 26 }}
            className="overflow-hidden shrink-0 h-full"
            style={{
              // Glassier — was 0.92 opaque, now lets the panel halo through.
              background: "rgba(10,10,12,0.45)",
              backdropFilter: "blur(18px) saturate(140%)",
              WebkitBackdropFilter: "blur(18px) saturate(140%)",
              borderRight: "1px solid rgba(255,255,255,0.06)",
            }}
          >
            <div
              className="w-[220px] flex flex-col h-full overflow-y-auto py-4 px-3 gap-5 scrollbar-none"
              style={{
                // Hide the scrollbar visually but keep scroll wheel
                // functional. The operator complained about the visible track stripe
                // breaking the panel aesthetic.
                scrollbarWidth: "none",
                msOverflowStyle: "none",
              } as React.CSSProperties}
            >

              {/* Search */}
              <div>
                <SectionHeader>Search</SectionHeader>
                <div className="relative">
                  <Search
                    className="absolute left-2 top-1/2 -translate-y-1/2 pointer-events-none"
                    style={{ width: "11px", height: "11px", color: "var(--color-text-muted)" }}
                  />
                  <input
                    type="text"
                    value={searchQuery}
                    onChange={(e) => onSearchChange?.(e.target.value)}
                    placeholder="node label…"
                    className="w-full rounded px-2 py-1 pl-6 font-mono text-[11px] outline-none"
                    style={{
                      background: "rgba(255,255,255,0.04)",
                      border: "1px solid rgba(255,255,255,0.08)",
                      color: "var(--color-text-primary)",
                    }}
                    aria-label="Search node labels"
                  />
                </div>
              </div>

              {/* Type chips */}
              <div>
                <SectionHeader>Type</SectionHeader>
                <div className="flex flex-col gap-0.5">
                  {VAULT_TYPES.map((type) => (
                    <TypeChip
                      key={type}
                      type={type}
                      selected={selectedTypes.includes(type)}
                      onToggle={toggleType}
                    />
                  ))}
                </div>
              </div>

              {/* Agent chips */}
              {agents.length > 0 && (
                <div>
                  <SectionHeader>Agent</SectionHeader>
                  <div className="flex flex-col gap-0.5">
                    {agents.map((agent) => (
                      <AgentChip
                        key={agent}
                        agent={agent}
                        selected={selectedAgents.includes(agent)}
                        onToggle={toggleAgent}
                      />
                    ))}
                  </div>
                </div>
              )}

              {/* Overlay toggles */}
              <div>
                <SectionHeader>Overlays</SectionHeader>
                <div className="flex flex-col gap-0.5">
                  <ToggleRow
                    label="Heatmap"
                    enabled={showHeatmap}
                    onToggle={onHeatmapToggle}
                  />
                  <ToggleRow
                    label="Clusters"
                    enabled={showClusters}
                    onToggle={onClustersToggle}
                  />
                </div>
              </div>

              {/* Reset */}
              <div className="mt-auto pt-2" style={{ borderTop: "1px solid rgba(255,255,255,0.05)" }}>
                <button
                  type="button"
                  onClick={handleReset}
                  disabled={!hasActiveFilters}
                  className="flex items-center gap-1.5 w-full rounded px-2 py-1.5 font-mono text-[11px] transition-opacity"
                  style={{
                    background: hasActiveFilters ? "rgba(239,68,68,0.08)" : "transparent",
                    border: hasActiveFilters
                      ? "1px solid rgba(239,68,68,0.2)"
                      : "1px solid transparent",
                    color: hasActiveFilters
                      ? C.error
                      : "var(--color-text-muted)",
                    opacity: hasActiveFilters ? 1 : 0.4,
                    cursor: hasActiveFilters ? "pointer" : "default",
                  }}
                  aria-label="Reset all filters"
                >
                  <RotateCcw style={{ width: "10px", height: "10px" }} />
                  Reset filters
                </button>
              </div>
            </div>
          </motion.aside>
        )}
      </AnimatePresence>

      {/* Collapse toggle button */}
      <button
        type="button"
        onClick={() => setCollapsed((c) => !c)}
        className="absolute -right-3 top-1/2 -translate-y-1/2 z-10 flex items-center justify-center rounded-full"
        style={{
          width: "20px",
          height: "20px",
          background: "rgba(26,26,26,0.95)",
          border: "1px solid rgba(255,255,255,0.1)",
          color: "var(--color-text-secondary)",
          cursor: "pointer",
          boxShadow: "0 2px 8px rgba(0,0,0,0.4)",
        }}
        aria-label={collapsed ? "Expand filter sidebar" : "Collapse filter sidebar"}
      >
        {collapsed ? (
          <ChevronRight style={{ width: "11px", height: "11px" }} />
        ) : (
          <ChevronLeft style={{ width: "11px", height: "11px" }} />
        )}
      </button>
    </div>
  );
}
