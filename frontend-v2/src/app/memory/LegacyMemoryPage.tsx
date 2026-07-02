"use client";

import { useState, useEffect, useRef } from "react";
import AppShell from "@/components/layout/AppShell";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { motion, AnimatePresence } from "framer-motion";
import ReactMarkdown from "react-markdown";
import {
  Brain, Plus, Pin, Pencil, Trash2, X, ChevronRight,
  Search, BookOpen, Clock, RefreshCw, Check, Calendar,
} from "lucide-react";
import { api } from "@/lib/api";
import { useAppStore } from "@/lib/store";
import { timeAgo } from "@/lib/utils";
import type { BoardMemory, MemoryType } from "@/lib/types";
import { MemoryQueryBar } from "@/components/memory/MemoryQueryBar";
import { MemoryLayerTabs, type MemoryLayer } from "@/components/memory/MemoryLayerTabs";
import { EpisodicTimeline } from "@/components/memory/EpisodicTimeline";
import { SemanticCardGrid } from "@/components/memory/SemanticCardGrid";
import { AgentLessonMatrix } from "@/components/memory/AgentLessonMatrix";
import { AttachmentPanel } from "@/components/memory/AttachmentPanel";
import { MergeResolutionPanel } from "@/components/memory/MergeResolutionPanel";
import { C as _C, STATUS_TEXT } from "@/lib/colors";

// ── Design tokens — local aliases mapping to lib/colors ──────────────────────
const C = {
  bg:          "rgba(255,255,255,0.03)",
  bgHover:     "rgba(255,255,255,0.05)",
  border:      _C.border,
  borderSubtle:_C.borderSubtle,
  accent:      _C.accent,
  accentSoft:  _C.accentSubtle,
  online:      _C.online,
  warn:        _C.warning,
  err:         _C.error,
  info:        _C.info,
} as const;

const TYPE_CONFIG: Record<MemoryType, { color: string; label: string; pill: string }> = {
  lesson:        { color: C.err,    label: "Lesson",        pill: "rgba(239,68,68,0.12)" },
  reference:     { color: C.warn,   label: "Reference",     pill: "rgba(245,158,11,0.12)" },
  journal:       { color: C.online, label: "Journal",       pill: "rgba(0,204,136,0.10)" },
  knowledge:     { color: _C.textSecondary, label: "Knowledge",    pill: `${_C.textSecondary}1F` },
  weekly_review: { color: _C.textSecondary, label: "Weekly",       pill: `${_C.textSecondary}1F` },
  research:      { color: "#5E9EF7", label: "Research",     pill: "rgba(94,158,247,0.12)" },
  insight:       { color: C.online, label: "Insight",       pill: "rgba(0,204,136,0.10)" },
};

// Date range options
const DATE_RANGES = [
  { label: "Alle", value: "" },
  { label: "Heute", value: "1" },
  { label: "7 Tage", value: "7" },
  { label: "30 Tage", value: "30" },
  { label: "90 Tage", value: "90" },
];

function isWithinDays(dateStr: string, days: number): boolean {
  if (!dateStr) return false;
  try {
    const itemTime = new Date(dateStr).getTime();
    if (isNaN(itemTime)) return false;
    const cutoffTime = Date.now() - days * 24 * 60 * 60 * 1000;
    return itemTime >= cutoffTime;
  } catch {
    return false;
  }
}

function filterByDate(items: BoardMemory[], dateRange: string): BoardMemory[] {
  if (!dateRange) return items;
  const days = parseInt(dateRange, 10);
  return items.filter((m) => isWithinDays(m.created_at, days));
}

function hasActiveFilters(search: string, filterType: string, dateRange: string): boolean {
  return !!search || !!filterType || !!dateRange;
}

function TypePill({ type }: { type: MemoryType }) {
  const cfg = TYPE_CONFIG[type] ?? TYPE_CONFIG.knowledge;
  return (
    <span
      className="inline-flex items-center px-2 py-0.5 rounded-full text-[10px] font-semibold"
      style={{ background: cfg.pill, color: cfg.color }}
    >
      {cfg.label}
    </span>
  );
}

// ── Markdown renderer ─────────────────────────────────────────────────────────
function MarkdownContent({ content }: { content: string }) {
  return (
    <ReactMarkdown
      components={{
        h1: ({ children }) => <h1 className="text-lg font-bold mb-3 mt-4" style={{ color: "var(--color-text-primary)" }}>{children}</h1>,
        h2: ({ children }) => <h2 className="text-base font-semibold mb-2 mt-4" style={{ color: "var(--color-text-primary)" }}>{children}</h2>,
        h3: ({ children }) => <h3 className="text-sm font-semibold mb-1.5 mt-3" style={{ color: "var(--color-text-primary)" }}>{children}</h3>,
        p: ({ children }) => <p className="mb-3 leading-relaxed" style={{ color: "var(--color-text-body)" }}>{children}</p>,
        ul: ({ children }) => <ul className="mb-3 pl-4 space-y-1" style={{ color: "var(--color-text-body)" }}>{children}</ul>,
        ol: ({ children }) => <ol className="mb-3 pl-4 space-y-1 list-decimal" style={{ color: "var(--color-text-body)" }}>{children}</ol>,
        li: ({ children }) => <li className="text-sm leading-relaxed list-disc">{children}</li>,
        code: ({ children, className }) => {
          const isBlock = className?.includes("language-");
          return isBlock ? (
            <code className="block px-4 py-3 rounded-lg text-xs font-mono mb-3 overflow-x-auto"
              style={{ background: "rgba(255,255,255,0.05)", color: _C.accent, border: "1px solid rgba(255,255,255,0.08)" }}>
              {children}
            </code>
          ) : (
            <code className="px-1.5 py-0.5 rounded text-xs font-mono"
              style={{ background: _C.accentSubtle, color: _C.accent }}>
              {children}
            </code>
          );
        },
        blockquote: ({ children }) => (
          <blockquote className="pl-4 mb-3 text-sm italic" style={{ border: `1px solid ${_C.borderAccent}`, borderRadius: 4, background: _C.accentSubtle, paddingLeft: "0.75rem", color: "var(--color-text-secondary)" }}>
            {children}
          </blockquote>
        ),
        strong: ({ children }) => <strong className="font-semibold" style={{ color: "var(--color-text-primary)" }}>{children}</strong>,
        hr: () => <hr className="my-4" style={{ borderColor: "rgba(255,255,255,0.08)" }} />,
        a: ({ href, children }) => (
          <a href={href} target="_blank" rel="noopener noreferrer" className="underline" style={{ color: C.accent }}>
            {children}
          </a>
        ),
      }}
    >
      {content}
    </ReactMarkdown>
  );
}

// ── Filter bar ────────────────────────────────────────────────────────────────
function FilterBar({
  search, onSearch,
  filterType, onType,
  dateRange, onDate,
}: {
  search: string; onSearch: (v: string) => void;
  filterType: MemoryType | ""; onType: (v: MemoryType | "") => void;
  dateRange: string; onDate: (v: string) => void;
}) {
  return (
    <div className="flex gap-2 mb-4 flex-wrap">
      {/* Search */}
      <div className="relative flex-1 min-w-[180px]">
        <Search size={13} className="absolute left-3 top-1/2 -translate-y-1/2 pointer-events-none" style={{ color: "var(--color-text-muted)" }} />
        <input
          type="text"
          value={search}
          onChange={(e) => onSearch(e.target.value)}
          placeholder="Suchen..."
          className="w-full pl-8 pr-3 py-2 text-sm rounded-xl outline-none"
          style={{ background: C.bg, border: `1px solid ${C.border}`, color: "var(--color-text-primary)" }}
        />
      </div>

      {/* Type filter */}
      <select
        value={filterType}
        onChange={(e) => onType(e.target.value as MemoryType | "")}
        className="px-3 py-2 text-sm rounded-xl cursor-pointer outline-none"
        style={{ background: C.bg, border: `1px solid ${C.border}`, color: "var(--color-text-secondary)" }}
      >
        <option value="">Alle Typen</option>
        {(Object.keys(TYPE_CONFIG) as MemoryType[]).map((t) => (
          <option key={t} value={t}>{TYPE_CONFIG[t].label}</option>
        ))}
      </select>

      {/* Date filter */}
      <div className="flex items-center gap-1 px-1 rounded-xl" style={{ background: C.bg, border: `1px solid ${C.border}` }}>
        <Calendar size={13} className="ml-2 shrink-0" style={{ color: "var(--color-text-muted)" }} />
        {DATE_RANGES.map((r) => (
          <button
            key={r.value}
            onClick={() => onDate(r.value)}
            className="px-2.5 py-1.5 text-xs rounded-lg cursor-pointer transition-colors"
            style={{
              background: dateRange === r.value ? _C.accentSubtle : "transparent",
              color: dateRange === r.value ? _C.accent : "var(--color-text-muted)",
            }}
          >
            {r.label}
          </button>
        ))}
      </div>
    </div>
  );
}

// ── Memory Modal ─────────────────────────────────────────────────────────────
function MemoryModal({
  entry,
  onClose,
  boardId,
}: {
  entry: BoardMemory | null;
  onClose: () => void;
  boardId: string;
}) {
  const qc = useQueryClient();
  const [mode, setMode] = useState<"view" | "edit">("view");
  const [editTitle, setEditTitle] = useState("");
  const [editContent, setEditContent] = useState("");
  const [editType, setEditType] = useState<MemoryType>("knowledge");
  const [dirty, setDirty] = useState(false);
  const [showUnsaved, setShowUnsaved] = useState(false);
  const [saved, setSaved] = useState(false);
  const [activeWriteTab, setActiveWriteTab] = useState<"write" | "preview">("write");
  const backdropRef = useRef<HTMLDivElement>(null);

  const isNew = !entry?.id;

  useEffect(() => {
    if (entry) {
      setEditTitle(entry.title ?? "");
      setEditContent(entry.content);
      setEditType(entry.memory_type);
      setMode(isNew ? "edit" : "view");
      setDirty(false);
      setSaved(false);
    }
  }, [entry, isNew]);

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        if (dirty) setShowUnsaved(true);
        else onClose();
      }
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [dirty, onClose]);

  const updateMutation = useMutation({
    mutationFn: ({ id, data }: { id: string; data: Partial<BoardMemory> }) =>
      api.memory.update(boardId, id, data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["memory", boardId] });
      qc.invalidateQueries({ queryKey: ["knowledge"] });
      setSaved(true);
      setDirty(false);
      setMode("view");
      setTimeout(() => setSaved(false), 2500);
    },
  });

  const createMutation = useMutation({
    mutationFn: (data: { title: string; content: string; memory_type: MemoryType }) =>
      api.memory.create(boardId, data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["memory", boardId] });
      qc.invalidateQueries({ queryKey: ["knowledge"] });
      onClose();
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => api.memory.delete(boardId, id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["memory", boardId] });
      qc.invalidateQueries({ queryKey: ["knowledge"] });
      onClose();
    },
  });

  const pinMutation = useMutation({
    mutationFn: ({ id, pinned }: { id: string; pinned: boolean }) =>
      api.memory.update(boardId, id, { is_pinned: pinned }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["memory", boardId] }),
  });

  function handleSave() {
    const data = { title: editTitle, content: editContent, memory_type: editType };
    if (isNew) createMutation.mutate(data);
    else updateMutation.mutate({ id: entry!.id, data });
  }

  function tryClose() {
    if (dirty) { setShowUnsaved(true); return; }
    onClose();
  }

  if (!entry) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center" onClick={(e) => { if (e.target === backdropRef.current) tryClose(); }}>
      <div ref={backdropRef} className="absolute inset-0 bg-black/70" style={{ backdropFilter: "blur(8px)" }} onClick={tryClose} />

      <motion.div
        initial={{ scale: 0.94, opacity: 0 }}
        animate={{ scale: 1, opacity: 1 }}
        exit={{ scale: 0.94, opacity: 0 }}
        transition={{ duration: 0.22, ease: [0.16, 1, 0.3, 1] }}
        className="relative z-10 flex flex-col rounded-2xl overflow-hidden"
        style={{
          width: "min(680px, 94vw)",
          maxHeight: "88vh",
          background: _C.bgBase,
          border: `1px solid ${C.border}`,
          boxShadow: "0 4px 24px rgba(0,0,0,0.5), 0 1px 2px rgba(0,0,0,0.3)",
        }}
        onClick={(e) => e.stopPropagation()}
      >
        {/* Top highlight */}
        <div className="absolute inset-x-0 top-0 h-px pointer-events-none" style={{ background: "linear-gradient(90deg, transparent, rgba(255,255,255,0.12) 50%, transparent)" }} />

        {/* Header */}
        <div className="flex items-center gap-2.5 px-5 py-4 border-b shrink-0" style={{ borderColor: C.borderSubtle }}>
          {mode === "view" && <TypePill type={entry.memory_type} />}
          {mode === "view" && entry.is_pinned && (
            <span className="text-xs" style={{ color: "var(--color-text-muted)" }}>📌</span>
          )}
          <div className="flex-1" />

          {mode === "view" && (
            <div className="flex items-center gap-1.5">
              <button
                onClick={() => { setEditTitle(entry.title ?? ""); setEditContent(entry.content); setEditType(entry.memory_type); setMode("edit"); setDirty(false); }}
                className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs cursor-pointer transition-colors"
                style={{ background: C.bg, border: `1px solid ${C.border}`, color: "var(--color-text-secondary)" }}
                onMouseEnter={(e) => (e.currentTarget.style.color = "var(--color-text-primary)")}
                onMouseLeave={(e) => (e.currentTarget.style.color = "var(--color-text-secondary)")}
              >
                <Pencil size={12} /> Bearbeiten
              </button>
              {!isNew && (
                <>
                  <button
                    onClick={() => pinMutation.mutate({ id: entry.id, pinned: !entry.is_pinned })}
                    className="p-1.5 rounded-lg cursor-pointer transition-colors"
                    style={{ background: C.bg, border: `1px solid ${C.border}`, color: entry.is_pinned ? C.warn : "var(--color-text-muted)" }}
                    title={entry.is_pinned ? "Entpinnen" : "Anpinnen"}
                  >
                    <Pin size={13} />
                  </button>
                  <button
                    onClick={() => { if (confirm(`"${entry.title}" löschen?`)) deleteMutation.mutate(entry.id); }}
                    className="p-1.5 rounded-lg cursor-pointer transition-colors"
                    style={{ background: "rgba(239,68,68,0.08)", border: "1px solid rgba(239,68,68,0.2)", color: C.err }}
                  >
                    <Trash2 size={13} />
                  </button>
                </>
              )}
              <button onClick={tryClose} className="p-1.5 rounded-lg cursor-pointer transition-colors ml-1" style={{ color: "var(--color-text-muted)" }}
                onMouseEnter={(e) => (e.currentTarget.style.color = "var(--color-text-primary)")}
                onMouseLeave={(e) => (e.currentTarget.style.color = "var(--color-text-muted)")}
              >
                <X size={16} />
              </button>
            </div>
          )}

          {mode === "edit" && (
            <div className="flex items-center gap-1.5">
              <button onClick={() => { if (dirty) setShowUnsaved(true); else setMode("view"); }} className="px-3 py-1.5 text-xs rounded-lg cursor-pointer" style={{ color: "var(--color-text-secondary)" }}>
                Abbrechen
              </button>
              <button
                onClick={handleSave}
                disabled={updateMutation.isPending || createMutation.isPending}
                className="px-4 py-1.5 text-xs rounded-lg cursor-pointer font-medium"
                style={{ background: `linear-gradient(135deg, ${C.accent}, ${_C.accentHover})`, color: _C.bgDeep }}
              >
                {(updateMutation.isPending || createMutation.isPending) ? "..." : "Speichern"}
              </button>
              <button onClick={tryClose} className="p-1.5 rounded-lg cursor-pointer ml-1" style={{ color: "var(--color-text-muted)" }}>
                <X size={16} />
              </button>
            </div>
          )}
        </div>

        {/* View mode */}
        {mode === "view" && (
          <div className="flex-1 overflow-y-auto px-6 py-5">
            <h2 className="text-xl font-bold tracking-tight mb-3" style={{ color: "var(--color-text-primary)" }}>
              {entry.title || "(Kein Titel)"}
            </h2>
            <div className="flex gap-3 flex-wrap text-xs mb-5" style={{ color: "var(--color-text-muted)" }}>
              <span>{timeAgo(entry.created_at)}</span>
              {entry.auto_generated && <span>· Auto-generiert</span>}
              {entry.source && <span>· {entry.source}</span>}
            </div>
            <div
              className="text-sm rounded-xl p-4"
              style={{ background: "rgba(255,255,255,0.02)", border: `1px solid ${C.borderSubtle}` }}
            >
              {entry.content
                ? <MarkdownContent content={entry.content} />
                : <em style={{ color: "var(--color-text-muted)" }}>Kein Inhalt</em>
              }
            </div>
            {/* Phase 5 MSY-02: cosine-merge candidate resolution panel.
                Renders only when entry.merge_candidate_id !== null.
                onResolved closes the modal so the freshly invalidated
                queries refetch and the badge disappears from the card list. */}
            {entry.merge_candidate_id != null && (
              <MergeResolutionPanel entry={entry} onResolved={onClose} />
            )}
            {entry.tags?.length > 0 && (
              <div className="flex gap-1.5 flex-wrap mt-4">
                {entry.tags.map((t) => (
                  <span key={t} className="px-2 py-0.5 rounded-full text-[10px]" style={{ background: "rgba(255,255,255,0.06)", color: "var(--color-text-muted)" }}>{t}</span>
                ))}
              </div>
            )}
            {/* Phase 5 MSY-03: attachments — read-only in v0.5.
                editMode hardcoded false; in-app upload UI deferred to a
                follow-up plan that wires modal-edit-mode through. Backend
                POST/DELETE endpoints remain functional. */}
            <AttachmentPanel entry={entry} editMode={false} />
          </div>
        )}

        {/* Edit mode */}
        {mode === "edit" && (
          <div className="flex-1 overflow-y-auto px-6 py-5 flex flex-col gap-4">
            {/* Type picker */}
            <div>
              <label className="block text-[11px] font-semibold uppercase tracking-wider mb-2" style={{ color: "var(--color-text-muted)" }}>Typ</label>
              <div className="flex gap-1.5 flex-wrap">
                {(Object.keys(TYPE_CONFIG) as MemoryType[]).map((t) => (
                  <button
                    key={t}
                    onClick={() => { setEditType(t); setDirty(true); }}
                    className="px-3 py-1 rounded-full text-[11px] font-medium cursor-pointer transition-all"
                    style={{
                      border: editType === t ? `1px solid ${C.accent}` : `1px solid ${C.border}`,
                      background: editType === t ? C.accentSoft : "transparent",
                      color: editType === t ? C.accent : "var(--color-text-muted)",
                    }}
                  >
                    {TYPE_CONFIG[t].label}
                  </button>
                ))}
              </div>
            </div>

            {/* Title */}
            <div>
              <label className="block text-[11px] font-semibold uppercase tracking-wider mb-1.5" style={{ color: "var(--color-text-muted)" }}>Titel</label>
              <input
                type="text"
                value={editTitle}
                onChange={(e) => { setEditTitle(e.target.value); setDirty(true); }}
                placeholder="Titel des Eintrags..."
                className="w-full px-4 py-2.5 rounded-xl text-sm font-medium outline-none transition-colors"
                style={{ background: "rgba(255,255,255,0.03)", border: `1px solid ${C.border}`, color: "var(--color-text-primary)" }}
                onFocus={(e) => (e.currentTarget.style.borderColor = _C.accent)}
                onBlur={(e) => (e.currentTarget.style.borderColor = C.border)}
              />
            </div>

            {/* Content with write/preview tab */}
            <div className="flex-1">
              <div className="flex items-center justify-between mb-1.5">
                <label className="text-[11px] font-semibold uppercase tracking-wider" style={{ color: "var(--color-text-muted)" }}>Inhalt</label>
                <div className="flex gap-0.5 p-0.5 rounded-lg" style={{ background: "rgba(255,255,255,0.05)" }}>
                  {(["write", "preview"] as const).map((tab) => (
                    <button
                      key={tab}
                      onClick={() => setActiveWriteTab(tab)}
                      className="px-3 py-1 text-[11px] rounded-md cursor-pointer transition-colors"
                      style={{
                        background: activeWriteTab === tab ? "rgba(255,255,255,0.08)" : "transparent",
                        color: activeWriteTab === tab ? "var(--color-text-primary)" : "var(--color-text-muted)",
                      }}
                    >
                      {tab === "write" ? "Schreiben" : "Vorschau"}
                    </button>
                  ))}
                </div>
              </div>
              {activeWriteTab === "write" ? (
                <textarea
                  value={editContent}
                  onChange={(e) => { setEditContent(e.target.value); setDirty(true); }}
                  rows={10}
                  placeholder="Markdown wird unterstützt..."
                  className="w-full px-4 py-3 rounded-xl text-sm outline-none resize-y transition-colors"
                  style={{
                    background: "rgba(255,255,255,0.02)",
                    border: `1px solid ${C.border}`,
                    color: "var(--color-text-body)",
                    fontFamily: "ui-monospace, monospace",
                    lineHeight: 1.6,
                    minHeight: 200,
                  }}
                  onFocus={(e) => (e.currentTarget.style.borderColor = _C.accent)}
                  onBlur={(e) => (e.currentTarget.style.borderColor = C.border)}
                />
              ) : (
                <div
                  className="px-4 py-3 rounded-xl text-sm"
                  style={{
                    background: "rgba(255,255,255,0.02)",
                    border: `1px solid ${C.borderSubtle}`,
                    minHeight: 200,
                  }}
                >
                  {editContent
                    ? <MarkdownContent content={editContent} />
                    : <em style={{ color: "var(--color-text-muted)" }}>Kein Inhalt</em>
                  }
                </div>
              )}
            </div>

            {/* Unsaved warning */}
            <AnimatePresence>
              {showUnsaved && (
                <motion.div
                  initial={{ opacity: 0, y: 4 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: 4 }}
                  className="flex items-center gap-3 px-4 py-3 rounded-xl text-xs"
                  style={{ background: "rgba(245,158,11,0.08)", border: "1px solid rgba(245,158,11,0.25)", color: C.warn }}
                >
                  ⚠ Ungespeicherte Änderungen — Trotzdem schliessen?
                  <button onClick={onClose} className="ml-auto underline cursor-pointer">Schliessen</button>
                  <button onClick={() => setShowUnsaved(false)} className="underline cursor-pointer" style={{ color: "var(--color-text-muted)" }}>Weiterschreiben</button>
                </motion.div>
              )}
            </AnimatePresence>
          </div>
        )}

        {/* Saved toast */}
        <AnimatePresence>
          {saved && (
            <motion.div
              initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }}
              className="absolute bottom-4 left-1/2 -translate-x-1/2 flex items-center gap-2 px-4 py-2 rounded-full text-xs font-medium pointer-events-none"
              style={{ background: "rgba(0,204,136,0.15)", border: "1px solid rgba(0,204,136,0.3)", color: C.online }}
            >
              <Check size={12} /> Gespeichert
            </motion.div>
          )}
        </AnimatePresence>
      </motion.div>
    </div>
  );
}

// ── Scope options for the dropdown ───────────────────────────────────────────
const SCOPE_OPTIONS = [
  { value: "all",    label: "Alle" },
  { value: "board",  label: "Board" },
  { value: "agent",  label: "Agent" },
  { value: "global", label: "Kein Kontext" },
] as const;

type Scope = (typeof SCOPE_OPTIONS)[number]["value"];

// ── Page ─────────────────────────────────────────────────────────────────────
/**
 * LegacyMemoryPage — board_memory data (pre-vault).
 * Preserved for coexistence during M.3. Will be deleted in M.5 cleanup.
 * Renamed from app/memory/page.tsx — do not delete until M.5.
 */
export default function LegacyMemoryPage() {
  const { activeBoardId } = useAppStore();
  const [activeLayer, setActiveLayer] = useState<MemoryLayer>("episodic");
  const [scope, setScope] = useState<Scope>("all");

  // Shared filter state across all tabs
  const [search, setSearch] = useState("");
  const [filterType, setFilterType] = useState<MemoryType | "">("");
  const [dateRange, setDateRange] = useState<string>("");

  const [selectedEntry, setSelectedEntry] = useState<BoardMemory | null>(null);
  const [showModal, setShowModal] = useState(false);

  const boardId = activeBoardId ?? "";

  // Build scope params for API calls (Phase 5 MSY-05).
  // "all" = no filter (legacy unconstrained behaviour, backend default).
  // "board" = filter to active board (board_id required).
  // "agent" = filter to agent-scoped entries only.
  // "global" = entries with no board_id AND no agent_id (true global store).
  const scopeParams =
    scope === "board" && boardId
      ? { scope: "board" as const, board_id: boardId }
      : scope === "agent"
      ? { scope: "agent" as const }
      : scope === "global"
      ? { scope: "global" as const }
      : { scope: "all" as const };

  // ── Data queries per layer ──
  const { data: episodicData, isLoading: episodicLoading } = useQuery({
    queryKey: ["knowledge-layer-episodic", scopeParams],
    queryFn: () => api.knowledge.listByLayer("episodic", { ...scopeParams, search: undefined }),
    enabled: activeLayer === "episodic",
    refetchInterval: 60_000,
  });

  const { data: semanticData, isLoading: semanticLoading } = useQuery({
    queryKey: ["knowledge-layer-semantic", scopeParams],
    queryFn: () => api.knowledge.listByLayer("semantic", { ...scopeParams, search: undefined }),
    enabled: activeLayer === "semantic",
    refetchInterval: 60_000,
  });

  const { data: agentLessons, isLoading: agentLoading } = useQuery({
    queryKey: ["knowledge-layer-agent"],
    queryFn: () => api.knowledge.listAgentLessons(),
    enabled: activeLayer === "agent",
    refetchInterval: 60_000,
  });

  // Counts for tabs (fetch all layers lightly)
  const { data: allEpisodic } = useQuery({
    queryKey: ["knowledge-layer-episodic-count"],
    queryFn: () => api.knowledge.listByLayer("episodic"),
    staleTime: 120_000,
  });
  const { data: allSemantic } = useQuery({
    queryKey: ["knowledge-layer-semantic-count"],
    queryFn: () => api.knowledge.listByLayer("semantic"),
    staleTime: 120_000,
  });
  const { data: allAgent } = useQuery({
    queryKey: ["knowledge-layer-agent-count"],
    queryFn: () => api.knowledge.listAgentLessons(),
    staleTime: 120_000,
  });

  const layerCounts = {
    episodic: allEpisodic?.length,
    semantic: allSemantic?.length,
    agent: allAgent?.length,
  };

  function openEntry(entry: BoardMemory) {
    setSelectedEntry(entry);
    setShowModal(true);
  }

  function openNew() {
    const defaultType: MemoryType = activeLayer === "episodic" ? "journal" : activeLayer === "agent" ? "lesson" : "knowledge";
    setSelectedEntry({
      id: "", board_id: boardId || null, agent_id: null, title: "", content: "",
      tags: [], source: "manual", memory_type: defaultType, is_pinned: false,
      auto_generated: false, linked_ids: [], created_at: "", updated_at: "",
    });
    setShowModal(true);
  }

  function applyFilters(items: BoardMemory[]): BoardMemory[] {
    return filterByDate(
      items.filter((m) => {
        const matchSearch = !search
          || m.title?.toLowerCase().includes(search.toLowerCase())
          || m.content.toLowerCase().includes(search.toLowerCase());
        const matchType = !filterType || m.memory_type === filterType;
        return matchSearch && matchType;
      }),
      dateRange
    );
  }

  const filteredEpisodic = applyFilters(episodicData ?? []);
  const filteredSemantic = applyFilters(semanticData ?? []);
  const filteredAgent = applyFilters(agentLessons ?? []);

  const isLoading = (activeLayer === "episodic" && episodicLoading)
    || (activeLayer === "semantic" && semanticLoading)
    || (activeLayer === "agent" && agentLoading);

  return (
    <AppShell>
      <div className="max-w-5xl mx-auto">
        {/* Header */}
        <div className="flex flex-col sm:flex-row sm:items-end sm:justify-between gap-3 mb-6">
          <div>
            <h1 className="text-2xl font-bold tracking-tight" style={{ color: "var(--color-text-primary)" }}>Memory</h1>
            <p className="text-sm mt-1" style={{ color: "var(--color-text-muted)" }}>
              3-Layer Memory System — Episodic · Semantic · Agent Lessons
            </p>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            {/* Scope dropdown */}
            <select
              value={scope}
              onChange={(e) => setScope(e.target.value as Scope)}
              className="px-3 py-2 text-sm rounded-xl cursor-pointer outline-none"
              style={{ background: C.bg, border: `1px solid ${C.border}`, color: "var(--color-text-secondary)" }}
            >
              {SCOPE_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>{opt.label}</option>
              ))}
            </select>
            <button
              onClick={openNew}
              className="flex items-center gap-2 px-4 py-2 rounded-xl text-sm font-medium cursor-pointer transition-all whitespace-nowrap"
              style={{ background: C.accentSoft, border: `1px solid ${_C.borderAccent}`, color: C.accent }}
            >
              <Plus size={14} /> Neuer Eintrag
            </button>
          </div>
        </div>

        {/* Semantic Memory Query — Qdrant Vektor-Suche ueber alle 3 Layer */}
        <MemoryQueryBar boardId={boardId || null} agentId={null} />

        {/* Layer Tabs */}
        <MemoryLayerTabs active={activeLayer} onChange={setActiveLayer} counts={layerCounts} />

        {/* Shared filters */}
        <FilterBar
          search={search} onSearch={setSearch}
          filterType={filterType} onType={setFilterType}
          dateRange={dateRange} onDate={setDateRange}
        />

        {/* Loading */}
        {isLoading && (
          <div className="flex items-center justify-center h-40">
            <RefreshCw size={18} className="animate-spin" style={{ color: C.accent }} />
          </div>
        )}

        {/* ── Episodic Layer ── */}
        {activeLayer === "episodic" && !isLoading && (
          <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} key="episodic">
            <EpisodicTimeline entries={filteredEpisodic} onOpen={openEntry} />
          </motion.div>
        )}

        {/* ── Semantic Layer ── */}
        {activeLayer === "semantic" && !isLoading && (
          <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} key="semantic">
            <SemanticCardGrid entries={filteredSemantic} onOpen={openEntry} onNew={openNew} />
          </motion.div>
        )}

        {/* ── Agent Layer ── */}
        {activeLayer === "agent" && !isLoading && (
          <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} key="agent">
            <AgentLessonMatrix lessons={filteredAgent} onOpen={openEntry} />
          </motion.div>
        )}
      </div>

      {/* Modal — uses knowledge API for global entries, board API for board-scoped */}
      <AnimatePresence>
        {showModal && selectedEntry && (
          <MemoryModal
            entry={selectedEntry}
            boardId={boardId}
            onClose={() => { setShowModal(false); setSelectedEntry(null); }}
          />
        )}
      </AnimatePresence>
    </AppShell>
  );
}

// ── Memory List component ─────────────────────────────────────────────────────
function MemoryList({ entries, onOpen }: { entries: BoardMemory[]; onOpen: (e: BoardMemory) => void }) {
  return (
    <div className="rounded-2xl overflow-hidden" style={{ border: `1px solid rgba(255,255,255,0.06)` }}>
      {entries.map((entry, i) => {
        const cfg = TYPE_CONFIG[entry.memory_type] ?? TYPE_CONFIG.knowledge;
        return (
          <div
            key={entry.id}
            onClick={() => onOpen(entry)}
            className="flex items-center justify-between px-4 py-3 cursor-pointer transition-colors"
            style={{
              borderBottom: i < entries.length - 1 ? `1px solid rgba(255,255,255,0.04)` : "none",
              background: "rgba(255,255,255,0.02)",
            }}
            onMouseEnter={(e) => (e.currentTarget.style.background = "rgba(255,255,255,0.04)")}
            onMouseLeave={(e) => (e.currentTarget.style.background = "rgba(255,255,255,0.02)")}
          >
            <div className="flex items-center gap-3 flex-1 min-w-0">
              <div className="w-2 h-2 rounded-full shrink-0" style={{ background: cfg.color }} />
              <div className="min-w-0">
                <div className="text-sm font-medium truncate" style={{ color: "var(--color-text-primary)" }}>
                  {entry.title || "(Kein Titel)"}
                </div>
                <div className="text-[11px] mt-0.5" style={{ color: "var(--color-text-muted)" }}>
                  {timeAgo(entry.created_at)}{entry.auto_generated ? " · Auto" : ""}
                </div>
              </div>
            </div>
            <div className="flex items-center gap-2 shrink-0">
              <TypePill type={entry.memory_type} />
              {entry.is_pinned && <span className="text-xs" style={{ color: "var(--color-text-muted)" }}>📌</span>}
              <ChevronRight size={14} style={{ color: "var(--color-text-muted)" }} />
            </div>
          </div>
        );
      })}
    </div>
  );
}
