"use client";

import { motion, AnimatePresence, useReducedMotion } from "framer-motion";
import { SlidersHorizontal, X } from "lucide-react";
import type { VaultNoteType } from "@/lib/types";
import type { VaultScope } from "@/hooks/useVaultSearch";
import { C } from "@/lib/colors";

// ── Types ─────────────────────────────────────────────────────────────────────

export interface VaultFilterState {
  scope?: VaultScope;
  agent?: string;
  type?: VaultNoteType;
}

interface VaultFilterStripProps {
  filters: VaultFilterState;
  agents: string[];        // list of known agent slugs from notes
  onChange: (next: Partial<VaultFilterState>) => void;
  /** Panel open state — lifted to parent so it can persist in localStorage. */
  open: boolean;
  onOpenChange: (next: boolean) => void;
}

// ── Constants ──────────────────────────────────────────────────────────────────

const SCOPES: { value: VaultScope; label: string }[] = [
  { value: "episodic", label: "episodic" },
  { value: "semantic", label: "semantic" },
  { value: "agents", label: "agents" },
];

const TYPES: { value: VaultNoteType; label: string }[] = [
  { value: "lesson", label: "lesson" },
  { value: "knowledge", label: "knowledge" },
  { value: "reference", label: "reference" },
  { value: "journal", label: "journal" },
  { value: "weekly_review", label: "weekly review" },
  { value: "note", label: "note" },
  { value: "deliverable", label: "files" },
];

const TYPE_LABELS: Record<string, string> = Object.fromEntries(
  TYPES.map((t) => [t.value, t.label])
);

// ── Chip ──────────────────────────────────────────────────────────────────────

function Chip({
  label,
  active,
  onClick,
}: {
  label: string;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      type="button"
      className="shrink-0 font-mono text-[11px] px-2.5 py-1 rounded-md cursor-pointer transition-colors"
      style={{
        background: active ? C.accentSubtle : "rgba(255,255,255,0.04)",
        color: active ? C.accent : "var(--color-text-muted)",
        border: active ? `1px solid ${C.borderAccent}` : "1px solid rgba(255,255,255,0.06)",
      }}
    >
      {active ? "● " : ""}
      {label}
    </button>
  );
}

// ── Label ─────────────────────────────────────────────────────────────────────

function StripLabel({ children }: { children: React.ReactNode }) {
  return (
    <span
      className="font-mono uppercase tracking-widest text-[10px] shrink-0 self-center"
      style={{ color: "var(--color-text-muted)" }}
    >
      {children}
    </span>
  );
}

// ── Active-filter pill (in the collapsed summary row) ───────────────────────────
// Shows a chosen filter with an inline × that clears just that dimension. Keeps
// the operator's context visible even while the panel is folded away.

function ActivePill({
  label,
  onRemove,
}: {
  label: string;
  onRemove: () => void;
}) {
  return (
    <span
      className="shrink-0 inline-flex items-center gap-1 font-mono text-[11px] pl-2 pr-1 py-0.5 rounded-md"
      style={{
        background: C.accentSubtle,
        color: C.accent,
        border: `1px solid ${C.borderAccent}`,
      }}
    >
      {label}
      <button
        type="button"
        onClick={onRemove}
        aria-label={`Filter ${label} entfernen`}
        className="flex items-center justify-center rounded cursor-pointer min-h-touch min-w-touch -my-2 -mr-1 hover:opacity-70 transition-opacity"
        style={{ color: C.accent }}
      >
        <X size={11} strokeWidth={2.5} />
      </button>
    </span>
  );
}

// ── VaultFilterStrip ──────────────────────────────────────────────────────────

export function VaultFilterStrip({
  filters,
  agents,
  onChange,
  open,
  onOpenChange,
}: VaultFilterStripProps) {
  const prefersReducedMotion = useReducedMotion();

  function toggleScope(scope: VaultScope) {
    onChange({ scope: filters.scope === scope ? undefined : scope });
  }

  function toggleAgent(agent: string) {
    onChange({ agent: filters.agent === agent ? undefined : agent });
  }

  function toggleType(type: VaultNoteType) {
    onChange({ type: filters.type === type ? undefined : type });
  }

  // Active filters → drives badge count + the collapsed summary pills.
  const activePills: { key: string; label: string; clear: () => void }[] = [];
  if (filters.scope) {
    activePills.push({
      key: "scope",
      label: filters.scope,
      clear: () => onChange({ scope: undefined }),
    });
  }
  if (filters.agent) {
    activePills.push({
      key: "agent",
      label: filters.agent,
      clear: () => onChange({ agent: undefined }),
    });
  }
  if (filters.type) {
    activePills.push({
      key: "type",
      label: TYPE_LABELS[filters.type] ?? filters.type,
      clear: () => onChange({ type: undefined }),
    });
  }
  const activeCount = activePills.length;

  // Reduced motion → instant; otherwise a short height/opacity ease-out.
  const panelTransition = prefersReducedMotion
    ? { duration: 0 }
    : { duration: 0.22, ease: [0.16, 1, 0.3, 1] as const };

  return (
    <div className="flex flex-col gap-2.5">
      {/* ── Toggle row: Filter button (+ badge) + collapsed active-filter summary ── */}
      <div className="flex items-center gap-2.5">
        <button
          type="button"
          onClick={() => onOpenChange(!open)}
          aria-expanded={open}
          aria-controls="vault-filter-panel"
          className="shrink-0 inline-flex items-center gap-2 min-h-touch px-3 rounded-md cursor-pointer transition-colors"
          style={{
            background: open || activeCount > 0 ? C.accentSubtle : "rgba(255,255,255,0.04)",
            color: open || activeCount > 0 ? C.accent : "var(--color-text-secondary)",
            border:
              open || activeCount > 0
                ? `1px solid ${C.borderAccent}`
                : "1px solid rgba(255,255,255,0.06)",
          }}
        >
          <SlidersHorizontal size={14} strokeWidth={2} />
          <span className="font-mono uppercase tracking-widest text-[11px]">Filter</span>
          {activeCount > 0 && (
            <span
              className="inline-flex items-center justify-center font-mono rounded-full"
              style={{
                minWidth: "16px",
                height: "16px",
                padding: "0 4px",
                fontSize: "10px",
                lineHeight: 1,
                background: C.accent,
                color: C.bgDeep,
              }}
            >
              {activeCount}
            </span>
          )}
        </button>

        {/* Collapsed summary — only the chosen filters, each removable. Hidden
            while the panel is open (the full chip rows already show them) and
            when nothing is active. Keeps the row to one line + horizontally
            scrollable on narrow viewports. */}
        {!open && activeCount > 0 && (
          <div
            className="flex items-center gap-1.5 overflow-x-auto scrollbar-none min-w-0"
            style={{ overscrollBehaviorX: "contain" }}
          >
            {activePills.map((p) => (
              <ActivePill key={p.key} label={p.label} onRemove={p.clear} />
            ))}
          </div>
        )}
      </div>

      {/* ── Expandable panel: the 3 chip rows ── */}
      <AnimatePresence initial={false}>
        {open && (
          <motion.div
            id="vault-filter-panel"
            key="filter-panel"
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={panelTransition}
            style={{ overflow: "hidden" }}
          >
            <div
              className="flex flex-col gap-2.5 pt-1 pb-4"
              style={{ borderBottom: "1px solid rgba(255,255,255,0.05)" }}
            >
              {/* SCOPE row */}
              <div
                className="flex items-center gap-2 overflow-x-auto scrollbar-none"
                style={{ overscrollBehaviorX: "contain" }}
              >
                <StripLabel>SCOPE</StripLabel>
                {SCOPES.map((s) => (
                  <Chip
                    key={s.value}
                    label={s.label}
                    active={filters.scope === s.value}
                    onClick={() => toggleScope(s.value)}
                  />
                ))}
              </div>

              {/* AGENT row — only shown when there are known agents */}
              {agents.length > 0 && (
                <div
                  className="flex items-center gap-2 overflow-x-auto scrollbar-none"
                  style={{ overscrollBehaviorX: "contain" }}
                >
                  <StripLabel>AGENT</StripLabel>
                  {agents.map((agent) => (
                    <Chip
                      key={agent}
                      label={agent}
                      active={filters.agent === agent}
                      onClick={() => toggleAgent(agent)}
                    />
                  ))}
                </div>
              )}

              {/* TYPE row */}
              <div
                className="flex items-center gap-2 overflow-x-auto scrollbar-none"
                style={{ overscrollBehaviorX: "contain" }}
              >
                <StripLabel>TYPE</StripLabel>
                {TYPES.map((t) => (
                  <Chip
                    key={t.value}
                    label={t.label}
                    active={filters.type === t.value}
                    onClick={() => toggleType(t.value)}
                  />
                ))}
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
