"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { motion, AnimatePresence } from "framer-motion";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronLeft, Check, Link2, Pencil, Trash2, X } from "lucide-react";
import { api } from "@/lib/api";
import type { VaultNote } from "@/lib/types";
import { C, STATUS_TEXT } from "@/lib/colors";
import { useVaultNote } from "@/hooks/useVaultNote";
import { useBodyScrollLock } from "@/hooks/useBodyScrollLock";
import { VaultMarkdown } from "./VaultMarkdown";
import { AttachmentPreview } from "./AttachmentPreview";
import {
  titleFromNote,
  parseTags,
  parseDateFromNote,
  formatLongDate,
} from "./VaultNoteRow";
import { colorForAgent } from "./agentColors";
import { ConfirmDeleteModal } from "./ConfirmDeleteModal";

// ── Phase E Task-Klammer: "Verwandt"-Sektion ──────────────────────────────────


function RelatedNotesSection({
  taskId,
  excludePath,
  onSelectNote,
}: {
  taskId: string;
  excludePath: string;
  onSelectNote?: (path: string) => void;
}) {
  const { data, isLoading } = useQuery({
    queryKey: ["vault", "related", taskId],
    queryFn: () => api.vault.related(taskId),
    staleTime: 30_000,
  });

  // Filter the current note out — the operator already sees it. If that leaves nothing,
  // don't render the section at all (no empty heading clutter).
  const others = (data?.notes ?? []).filter((n) => n.path !== excludePath);
  if (isLoading || others.length === 0) return null;

  return (
    <div
      className="mt-5 rounded-md"
      style={{
        background: C.accentSubtle,
        border: `1px solid ${C.borderAccent}`,
        padding: "10px 12px",
      }}
    >
      <div className="flex items-center gap-1.5 mb-1.5">
        <Link2
          size={11}
          style={{ color: C.accent }}
        />
        <span
          className="font-mono uppercase"
          style={{
            fontSize: "9.5px",
            letterSpacing: "0.14em",
            color: C.accent,
          }}
        >
          verwandt · {others.length}
        </span>
      </div>
      <ul className="space-y-1">
        {others.slice(0, 8).map((n) => (
          <li key={n.path}>
            <button
              type="button"
              onClick={() => onSelectNote?.(n.path)}
              disabled={!onSelectNote}
              className="w-full text-left flex items-center gap-2 rounded px-1.5 py-0.5 transition-colors hover:bg-white/[0.04] disabled:opacity-60 disabled:cursor-default"
              style={{ fontSize: "12px" }}
            >
              <span
                className="font-mono uppercase shrink-0"
                style={{
                  fontSize: "9px",
                  letterSpacing: "0.1em",
                  color: "var(--color-text-muted)",
                  minWidth: "60px",
                }}
              >
                {n.type}
              </span>
              <span
                className="truncate"
                style={{ color: "var(--color-text-secondary)" }}
              >
                {(typeof n.title === "string" && n.title.trim())
                  || n.path.split("/").pop()
                  || n.path}
              </span>
              {n.agent && (
                <span
                  className="font-mono shrink-0"
                  style={{
                    fontSize: "10px",
                    color: "var(--color-text-muted)",
                    marginLeft: "auto",
                  }}
                >
                  {n.agent}
                </span>
              )}
            </button>
          </li>
        ))}
        {others.length > 8 && (
          <li
            className="font-mono italic"
            style={{
              fontSize: "10px",
              color: "var(--color-text-muted)",
              paddingLeft: "1.5rem",
            }}
          >
            … und {others.length - 8} weitere
          </li>
        )}
      </ul>
    </div>
  );
}


// ── Breadcrumb ─────────────────────────────────────────────────────────────────

function Breadcrumb({ path }: { path: string }) {
  // e.g. "agents/sparky/lessons/rate-limits.md" →  AGENTS / SPARKY / LESSONS
  const parts = path.split("/").slice(0, -1); // drop filename
  return (
    <div
      className="font-mono uppercase tracking-widest"
      style={{ fontSize: "10px", color: "var(--color-text-muted)" }}
    >
      {parts.map((p, i) => (
        <span key={i}>
          {p}
          {i < parts.length - 1 && (
            <span style={{ color: "rgba(255,255,255,0.15)", margin: "0 6px" }}>
              /
            </span>
          )}
        </span>
      ))}
    </div>
  );
}

// ── Panel content ──────────────────────────────────────────────────────────────

function PanelContent({
  note,
  onWikilinkClick,
  onSelectNote,
  onDeleted,
}: {
  note: VaultNote;
  onWikilinkClick: (target: string) => void;
  /** Phase E task-klammer — invoked when the user clicks a row in the
   *  "Verwandt"-Sektion. Parent wires this to its handleSelectNote so the
   *  panel switches to the chosen related note. */
  onSelectNote?: (path: string) => void;
  onDeleted?: () => void;
}) {
  const { data, isLoading, isError } = useVaultNote(note.path);
  const agentColor = colorForAgent(note.agent);
  const tags = parseTags(note.tags);
  const [confirmOpen, setConfirmOpen] = useState(false);

  // Scroll-restoration: jump the content area to top whenever the user
  // opens a different note. Without this, switching notes inherits the
  // previous scroll position — the operator landed mid-paragraph instead of at
  // the title.
  const scrollRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (scrollRef.current) scrollRef.current.scrollTop = 0;
  }, [note.path]);

  // Title resolution: prefer the canonical frontmatter title from the detail
  // fetch, then fall back to titleFromNote (which handles snippet + path).
  const fmTitle =
    typeof data?.frontmatter?.title === "string" ? data.frontmatter.title : null;
  const title = fmTitle ?? titleFromNote(note);

  // Date resolution: detail frontmatter.date wins, then per-note fallback chain.
  const fmDate =
    typeof data?.frontmatter?.date === "string" ? data.frontmatter.date : null;
  const fallbackDate = parseDateFromNote(note);
  const displayDate = fmDate
    ? formatLongDate(fmDate)
    : fallbackDate
    ? `${fallbackDate.month} ${fallbackDate.day}, ${fallbackDate.year}`
    : null;

  // ── Edit mode ─────────────────────────────────────────────────────────────
  // Draft state lives next to the note in this component so switching notes
  // discards the unsaved edits (intentional — the operator expects fresh state per
  // selection). Esc / save-success exit edit mode.
  const [isEditing, setIsEditing] = useState(false);
  const [draftTitle, setDraftTitle] = useState("");
  const [draftBody, setDraftBody] = useState("");
  const [draftTags, setDraftTags] = useState("");
  const [editError, setEditError] = useState<string | null>(null);

  const queryClient = useQueryClient();
  const saveMutation = useMutation({
    mutationFn: (payload: { title: string; content: string; tags: string[] }) =>
      api.vault.update(note.path, payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["vault"] });
      setIsEditing(false);
      setEditError(null);
    },
    onError: (err: Error) => {
      setEditError(err.message || "Save failed");
    },
  });

  const startEdit = useCallback(() => {
    setDraftTitle(title);
    setDraftBody(data?.content ?? "");
    setDraftTags(tags.join(", "));
    setEditError(null);
    setIsEditing(true);
  }, [title, data?.content, tags]);

  const cancelEdit = useCallback(() => {
    setIsEditing(false);
    setEditError(null);
  }, []);

  const handleSave = useCallback(() => {
    const cleanedTags = draftTags
      .split(/[,\n]/)
      .map((t) => t.trim().replace(/^#/, ""))
      .filter(Boolean);
    saveMutation.mutate({
      title: draftTitle.trim() || title,
      content: draftBody,
      tags: cleanedTags,
    });
  }, [draftTitle, draftBody, draftTags, title, saveMutation]);

  // Reset edit mode when the user switches notes (path change).
  const lastPathRef = useRef(note.path);
  useEffect(() => {
    if (lastPathRef.current !== note.path) {
      lastPathRef.current = note.path;
      setIsEditing(false);
      setEditError(null);
    }
  }, [note.path]);

  // Cmd/Ctrl+S to save, Esc to cancel.
  useEffect(() => {
    if (!isEditing) return;
    const handler = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "s") {
        e.preventDefault();
        if (!saveMutation.isPending) handleSave();
      } else if (e.key === "Escape") {
        if (!saveMutation.isPending) cancelEdit();
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [isEditing, handleSave, cancelEdit, saveMutation.isPending]);

  return (
    <div className="flex flex-col h-full min-h-0">
      {/* Panel header — Editorial Codex masthead */}
      <div
        className="shrink-0 px-7 py-6"
        style={{ borderBottom: "1px solid rgba(255,255,255,0.06)" }}
      >
        <div className="flex items-start justify-between gap-3">
          <Breadcrumb path={note.path} />
          <div className="flex items-center gap-0.5 shrink-0 -mt-1 -mr-1">
            {isEditing ? (
              <>
                <button
                  type="button"
                  onClick={cancelEdit}
                  disabled={saveMutation.isPending}
                  aria-label="Cancel edit"
                  title="Cancel (Esc)"
                  className="rounded p-1.5 transition-colors"
                  style={{
                    color: "var(--color-text-muted)",
                    background: "transparent",
                    border: "none",
                    cursor: saveMutation.isPending ? "default" : "pointer",
                    opacity: saveMutation.isPending ? 0.4 : 1,
                  }}
                  onMouseEnter={(e) => {
                    (e.currentTarget as HTMLButtonElement).style.color =
                      "var(--color-text-primary)";
                  }}
                  onMouseLeave={(e) => {
                    (e.currentTarget as HTMLButtonElement).style.color =
                      "var(--color-text-muted)";
                  }}
                >
                  <X size={14} />
                </button>
                <button
                  type="button"
                  onClick={handleSave}
                  disabled={saveMutation.isPending}
                  aria-label="Save edit"
                  title="Save (⌘S)"
                  className="rounded p-1.5 transition-colors flex items-center gap-1.5"
                  style={{
                    color: C.online,
                    background: "rgba(52,211,153,0.08)",
                    border: "1px solid rgba(52,211,153,0.25)",
                    cursor: saveMutation.isPending ? "default" : "pointer",
                  }}
                >
                  {saveMutation.isPending ? (
                    <span
                      className="inline-block w-3 h-3 rounded-full border-[1.5px] border-t-transparent animate-spin"
                      style={{ borderColor: C.online, borderTopColor: "transparent" }}
                    />
                  ) : (
                    <Check size={13} />
                  )}
                </button>
              </>
            ) : (
              <>
                <button
                  type="button"
                  onClick={startEdit}
                  aria-label="Edit note"
                  title="Bearbeiten"
                  className="rounded p-1.5 transition-colors"
                  style={{
                    color: "var(--color-text-muted)",
                    background: "transparent",
                    border: "none",
                    cursor: "pointer",
                  }}
                  onMouseEnter={(e) => {
                    (e.currentTarget as HTMLButtonElement).style.color =
                      C.accent;
                    (e.currentTarget as HTMLButtonElement).style.background =
                      C.accentSubtle;
                  }}
                  onMouseLeave={(e) => {
                    (e.currentTarget as HTMLButtonElement).style.color =
                      "var(--color-text-muted)";
                    (e.currentTarget as HTMLButtonElement).style.background =
                      "transparent";
                  }}
                >
                  <Pencil size={13} />
                </button>
                <button
                  type="button"
                  onClick={() => setConfirmOpen(true)}
                  aria-label="Delete note"
                  className="rounded p-1.5 transition-colors"
                  style={{
                    color: "var(--color-text-muted)",
                    background: "transparent",
                    border: "none",
                    cursor: "pointer",
                  }}
                  onMouseEnter={(e) => {
                    (e.currentTarget as HTMLButtonElement).style.color = STATUS_TEXT.error;
                    (e.currentTarget as HTMLButtonElement).style.background =
                      "rgba(239,68,68,0.08)";
                  }}
                  onMouseLeave={(e) => {
                    (e.currentTarget as HTMLButtonElement).style.color =
                      "var(--color-text-muted)";
                    (e.currentTarget as HTMLButtonElement).style.background =
                      "transparent";
                  }}
                  title="Move note to trash"
                >
                  <Trash2 size={14} />
                </button>
              </>
            )}
          </div>
        </div>

        {/* Meta line: type chip (agent-tinted) + agent + date */}
        <div className="flex items-center flex-wrap gap-2.5 mt-3.5 mb-3">
          <span
            className="font-mono uppercase font-semibold rounded-sm"
            style={{
              fontSize: "9.5px",
              letterSpacing: "0.14em",
              padding: "3px 7px",
              background: `${agentColor}1A`,
              color: agentColor,
              border: `1px solid ${agentColor}38`,
              lineHeight: 1,
            }}
          >
            {note.type}
          </span>
          {note.agent && (
            <span
              className="font-mono lowercase"
              style={{
                fontSize: "10px",
                letterSpacing: "0.04em",
                color: "var(--color-text-muted)",
              }}
            >
              {note.agent}
            </span>
          )}
          {displayDate && (
            <>
              <span
                className="font-mono"
                style={{ fontSize: "10px", color: "rgba(255,255,255,0.2)" }}
              >
                ·
              </span>
              <span
                className="font-mono uppercase tabular-nums"
                style={{
                  fontSize: "10px",
                  letterSpacing: "0.14em",
                  color: "var(--color-text-secondary)",
                }}
              >
                {displayDate}
              </span>
            </>
          )}
        </div>

        {isEditing ? (
          <input
            type="text"
            value={draftTitle}
            onChange={(e) => setDraftTitle(e.target.value)}
            placeholder="Titel"
            className="w-full bg-transparent outline-none"
            style={{
              fontSize: "26px",
              fontWeight: 700,
              letterSpacing: "-0.015em",
              lineHeight: 1.15,
              color: "var(--color-text-primary)",
              borderBottom: `1px dashed ${C.borderAccent}`,
              paddingBottom: "4px",
            }}
            autoFocus
          />
        ) : (
          <h2
            className="font-bold leading-[1.15]"
            style={{
              fontSize: "26px",
              letterSpacing: "-0.015em",
              color: "var(--color-text-primary)",
            }}
          >
            {title}
          </h2>
        )}

        {/* Tags — flat, no chips (matches list row treatment) */}
        {isEditing ? (
          <div className="mt-4">
            <label
              className="font-mono uppercase"
              style={{
                fontSize: "9.5px",
                letterSpacing: "0.14em",
                color: "var(--color-text-muted)",
                display: "block",
                marginBottom: "4px",
              }}
            >
              tags (komma-getrennt)
            </label>
            <input
              type="text"
              value={draftTags}
              onChange={(e) => setDraftTags(e.target.value)}
              placeholder="z.B. wikis, karpathy, vault"
              className="w-full bg-transparent outline-none font-mono"
              style={{
                fontSize: "12px",
                color: "var(--color-text-secondary)",
                borderBottom: `1px dashed ${C.border}`,
                paddingBottom: "3px",
              }}
            />
          </div>
        ) : tags.length > 0 ? (
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 mt-4">
            {tags.map((tag) => (
              <span
                key={tag}
                className="font-mono"
                style={{
                  fontSize: "10.5px",
                  color: "var(--color-text-muted)",
                }}
              >
                #{tag}
              </span>
            ))}
          </div>
        ) : null}

        {/* Phase E "Verwandt"-Sektion. Sits between the title metadata and
            the content area so the operator sees the task context before reading the
            body. Hides itself if there are no other notes from the same task. */}
        {!isEditing &&
          typeof data?.frontmatter?.task === "string" && (
            <RelatedNotesSection
              taskId={String(data.frontmatter.task)}
              excludePath={note.path}
              onSelectNote={onSelectNote}
            />
          )}

        {/* Inline edit-error banner under header so it's always in view */}
        {isEditing && editError && (
          <div
            className="mt-3 rounded-md px-3 py-2 flex items-center gap-2"
            style={{
              background: "rgba(239,68,68,0.08)",
              border: "1px solid rgba(239,68,68,0.25)",
              fontSize: "12px",
              color: STATUS_TEXT.error,
            }}
          >
            <X size={12} />
            {editError}
          </div>
        )}
      </div>

      {/* Content area */}
      <div
        ref={scrollRef}
        className="flex-1 overflow-y-auto overflow-x-hidden px-6 py-6 scrollbar-none"
        style={{ scrollbarWidth: "none", msOverflowStyle: "none" } as React.CSSProperties}
      >
        {isLoading && !isEditing && (
          <div className="space-y-3 animate-pulse">
            {Array.from({ length: 8 }).map((_, i) => (
              <div
                key={i}
                className="h-3 rounded"
                style={{
                  background: "rgba(255,255,255,0.05)",
                  width: `${60 + Math.random() * 35}%`,
                }}
              />
            ))}
          </div>
        )}
        {isError && !isEditing && (
          <div
            className="text-sm"
            style={{ color: "var(--color-text-muted)" }}
          >
            Failed to load note content.
          </div>
        )}
        {isEditing ? (
          <textarea
            value={draftBody}
            onChange={(e) => setDraftBody(e.target.value)}
            placeholder="Markdown body — frontmatter wird automatisch erhalten."
            spellCheck={false}
            className="w-full h-full bg-transparent outline-none resize-none font-mono"
            style={{
              fontSize: "13.5px",
              lineHeight: 1.6,
              color: "var(--color-text-body)",
              minHeight: "60vh",
              tabSize: 2,
            }}
            onKeyDown={(e) => {
              // Tab inserts 2 spaces instead of leaving the textarea — markdown
              // lists / code need indents, the default tab behavior is hostile.
              if (e.key === "Tab" && !e.shiftKey) {
                e.preventDefault();
                const t = e.currentTarget;
                const start = t.selectionStart;
                const end = t.selectionEnd;
                const next = t.value.slice(0, start) + "  " + t.value.slice(end);
                setDraftBody(next);
                requestAnimationFrame(() => {
                  t.selectionStart = t.selectionEnd = start + 2;
                });
              }
            }}
          />
        ) : (
          data && (
            <>
              <VaultMarkdown
                content={data.content}
                onWikilinkClick={onWikilinkClick}
              />
              {/* Phase 5: inline preview of the binary attachment for
                  deliverable wrappers. Only deliverable kinds with a
                  mime hint get a preview — document/url have no binary. */}
              {typeof data.frontmatter?.deliverable_id === "string" &&
                typeof data.frontmatter?.attachment_mime === "string" && (
                  <AttachmentPreview
                    deliverableId={String(data.frontmatter.deliverable_id)}
                    mime={String(data.frontmatter.attachment_mime)}
                    sizeBytes={
                      typeof data.frontmatter.attachment_size === "number"
                        ? data.frontmatter.attachment_size
                        : undefined
                    }
                    title={
                      typeof data.frontmatter.title === "string"
                        ? data.frontmatter.title
                        : undefined
                    }
                  />
                )}
            </>
          )
        )}
      </div>

      <ConfirmDeleteModal
        path={confirmOpen ? note.path : null}
        title={title}
        onClose={() => setConfirmOpen(false)}
        onDeleted={onDeleted}
      />
    </div>
  );
}

// ── Mobile overlay (full-screen sheet) ─────────────────────────────────────────
//
// Full-screen reading sheet on mobile. Fixes over the original (the operator: "can't
// close an opened memory"):
//   1. Rendered through a portal to <body> so it escapes <main>'s stacking
//      context. The old in-flow overlay sat at z-50 but the fixed MobileNav
//      header (hamburger, z-40) still won hit-testing at the top-left — the
//      back button and the hamburger overlapped, so taps hit the menu, not
//      close. The portal + z-[100] makes the sheet own the whole screen.
//   2. Header gets `env(safe-area-inset-top)` top padding so the Back button
//      clears the Dynamic Island / notch — without it the only close affordance
//      sat at y≈12px, under the island on the operator's iPhone (untappable).
// Plus: iOS body-scroll-lock (M4) so the list behind can't scroll, and Escape
// as a secondary close path. Back button is a real ≥44px touch target (M6).

function MobileOverlayPanel({
  note,
  onClose,
  onWikilinkClick,
  onSelectNote,
  onDeleted,
}: {
  note: VaultNote;
  onClose?: () => void;
  onWikilinkClick: (target: string) => void;
  onSelectNote?: (path: string) => void;
  onDeleted?: () => void;
}) {
  // Portal target is only available client-side; mount-gate avoids SSR mismatch.
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);

  // iOS-safe scroll lock while the sheet is open (MOBILE-SPEC M4).
  useBodyScrollLock(true);

  // Escape closes the sheet — secondary path next to the visible Back button.
  useEffect(() => {
    if (!onClose) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [onClose]);

  if (!mounted) return null;

  return createPortal(
    <motion.div
      key="mobile-panel"
      initial={{ x: "100%", opacity: 0 }}
      animate={{ x: 0, opacity: 1 }}
      exit={{ x: "100%", opacity: 0 }}
      transition={{ type: "spring", stiffness: 300, damping: 35 }}
      className="fixed inset-0 z-[100] flex flex-col md:hidden"
      style={{ background: "var(--color-bg-base)" }}
      role="dialog"
      aria-modal="true"
      aria-label="Note detail"
    >
      {/* Back button — safe-area top padding pushes it below the Dynamic Island. */}
      <div
        className="flex items-center gap-2 px-4 shrink-0"
        style={{
          borderBottom: "1px solid rgba(255,255,255,0.06)",
          paddingTop: "calc(env(safe-area-inset-top) + 0.5rem)",
          paddingBottom: "0.5rem",
        }}
      >
        <button
          onClick={onClose}
          type="button"
          aria-label="Back to list"
          className="flex items-center gap-1 cursor-pointer min-h-touch -ml-2 px-2 rounded-md"
          style={{ color: "var(--color-text-secondary)", background: "none", border: "none" }}
        >
          <ChevronLeft size={18} />
          <span className="text-sm">back</span>
        </button>
      </div>

      <div className="flex-1 min-h-0 overflow-hidden">
        <PanelContent note={note} onWikilinkClick={onWikilinkClick} onSelectNote={onSelectNote} onDeleted={onDeleted} />
      </div>
    </motion.div>,
    document.body,
  );
}

// ── VaultReadingPanel (desktop) ────────────────────────────────────────────────

interface VaultReadingPanelProps {
  note: VaultNote | null;
  onClose?: () => void;
  onWikilinkClick: (target: string) => void;
  /** Phase E task-klammer — invoked when the user clicks a row in the
   *  "Verwandt"-Sektion. */
  onSelectNote?: (path: string) => void;
  /** If true, renders as full-screen mobile overlay (from right) */
  isMobileOverlay?: boolean;
  /** Optional: invoked when the note is successfully deleted. Parent
   *  should clear `note` so the reading panel collapses to empty-state. */
  onDeleted?: () => void;
}

export function VaultReadingPanel({
  note,
  onClose,
  onWikilinkClick,
  onSelectNote,
  isMobileOverlay = false,
  onDeleted,
}: VaultReadingPanelProps) {
  if (isMobileOverlay) {
    return (
      <AnimatePresence>
        {note && (
          <MobileOverlayPanel
            note={note}
            onClose={onClose}
            onWikilinkClick={onWikilinkClick}
            onSelectNote={onSelectNote}
            onDeleted={onDeleted}
          />
        )}
      </AnimatePresence>
    );
  }

  // Desktop: slide in from right within the panel column
  return (
    <AnimatePresence mode="wait">
      {note ? (
        <motion.div
          key={note.path}
          initial={{ opacity: 0, x: 8 }}
          animate={{ opacity: 1, x: 0 }}
          exit={{ opacity: 0, x: 8 }}
          transition={{ type: "spring", stiffness: 200, damping: 20 }}
          className="h-full min-h-0 overflow-hidden"
        >
          <PanelContent note={note} onWikilinkClick={onWikilinkClick} onSelectNote={onSelectNote} onDeleted={onDeleted} />
        </motion.div>
      ) : (
        <motion.div
          key="empty-panel"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          className="h-full flex items-center justify-center"
        >
          <div className="text-center">
            <div
              className="font-mono text-4xl mb-4 select-none"
              style={{ color: "rgba(255,255,255,0.06)" }}
            >
              ◈
            </div>
            <p
              className="font-mono text-[11px] uppercase tracking-widest"
              style={{ color: "var(--color-text-muted)" }}
            >
              select a note to read
            </p>
          </div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
