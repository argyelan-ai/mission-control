"use client";

/**
 * NoteSidePanel — slide-in right panel showing a clicked graph node's content.
 *
 * 380 px wide, absolute-positioned over the right edge of the graph viewport.
 * Spring slide-in (stiffness 200, damping 26) matching M.3 VaultReadingPanel pattern.
 *
 * Shows:
 *   - Breadcrumb (path segments, font-mono muted)
 *   - Agent dot + type badge
 *   - Title (path stem, bold)
 *   - Tags (font-mono chips)
 *   - Full Markdown via VaultMarkdown (reused from M.3 T8)
 *   - Wikilink clicks → onWikilinkClick → triggers TraversalAnimation in parent
 *
 * Null / loading / error states all handled.
 * Tracks view via useVaultNote (fire-and-forget heatmap update).
 *
 * prefers-reduced-motion: Framer Motion respects it automatically when
 * `@media (prefers-reduced-motion: reduce)` is set — no extra config needed
 * because we use `type: "spring"` which framer short-circuits to instant
 * transitions in reduced-motion environments.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { useBodyScrollLock } from "@/hooks/useBodyScrollLock";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Link2, Pencil, Trash2, X } from "lucide-react";

import { api } from "@/lib/api";
import { useVaultNote } from "@/hooks/useVaultNote";
import { VaultMarkdown } from "@/components/vault/VaultMarkdown";
import { colorForAgent } from "@/components/vault/agentColors";
import { ConfirmDeleteModal } from "@/components/vault/ConfirmDeleteModal";
import { C, STATUS_TEXT } from "@/lib/colors";

// ── Phase E "Verwandt"-Sektion ────────────────────────────────────────────────


function RelatedNotesMini({
  taskId,
  excludePath,
  onSelect,
}: {
  taskId: string;
  excludePath: string;
  onSelect: (path: string) => void;
}) {
  const { data, isLoading } = useQuery({
    queryKey: ["vault", "related", taskId],
    queryFn: () => api.vault.related(taskId),
    staleTime: 30_000,
  });

  const others = (data?.notes ?? []).filter((n) => n.path !== excludePath);
  if (isLoading || others.length === 0) return null;

  return (
    <div
      className="mt-2 rounded-md"
      style={{
        background: C.accentSubtle,
        border: `1px solid ${C.borderAccent}`,
        padding: "6px 8px",
      }}
    >
      <div className="flex items-center gap-1 mb-1">
        <Link2 size={9} style={{ color: C.accent }} />
        <span
          className="font-mono uppercase"
          style={{
            fontSize: "8.5px",
            letterSpacing: "0.14em",
            color: C.accent,
          }}
        >
          verwandt · {others.length}
        </span>
      </div>
      <ul className="space-y-0.5">
        {others.slice(0, 6).map((n) => (
          <li key={n.path}>
            <button
              type="button"
              onClick={() => onSelect(n.path)}
              className="w-full text-left flex items-center gap-1.5 rounded px-1 py-0.5 transition-colors hover:bg-white/[0.05]"
              style={{ fontSize: "11px" }}
            >
              <span
                className="font-mono uppercase shrink-0"
                style={{
                  fontSize: "8px",
                  letterSpacing: "0.1em",
                  color: "var(--color-text-muted)",
                  minWidth: "52px",
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
            </button>
          </li>
        ))}
        {others.length > 6 && (
          <li
            className="font-mono italic"
            style={{ fontSize: "9px", color: "var(--color-text-muted)", paddingLeft: "1.2rem" }}
          >
            … +{others.length - 6}
          </li>
        )}
      </ul>
    </div>
  );
}


// ── Helpers ───────────────────────────────────────────────────────────────────

/** "agents/sparky/lessons/rate-limits.md" → "rate-limits" */
function titleFromPath(path: string): string {
  const stem = path.split("/").pop() ?? path;
  return stem.replace(/\.md$/i, "").replace(/[-_]/g, " ");
}

/** "agents/sparky/lessons/rate-limits.md" → "AGENTS / SPARKY / LESSONS" */
function breadcrumbFromPath(path: string): string {
  return path
    .split("/")
    .slice(0, -1)
    .map((p) => p.toUpperCase())
    .join(" / ");
}

/** "lesson decision" (space-joined FTS string) OR string[] → string[] */
function parseTags(tags: string | string[]): string[] {
  if (!tags) return [];
  if (Array.isArray(tags)) return tags.filter(Boolean);
  return tags.split(" ").filter(Boolean);
}

// ── Skeleton ──────────────────────────────────────────────────────────────────

function SkeletonNote() {
  return (
    <div className="space-y-3 animate-pulse pt-2">
      {[85, 65, 90, 55, 75, 80, 60].map((w, i) => (
        <div
          key={i}
          className="h-2.5 rounded"
          style={{
            background: "rgba(255,255,255,0.05)",
            width: `${w}%`,
          }}
        />
      ))}
    </div>
  );
}

// ── Main component ────────────────────────────────────────────────────────────

export interface NoteSidePanelProps {
  /** Vault-relative path of the selected note. null = panel closed. */
  path: string | null;
  onClose: () => void;
  /** Fires when user clicks a [[wikilink]] inside the note content. */
  onWikilinkClick: (targetPath: string) => void;
  className?: string;
}

export function NoteSidePanel({
  path,
  onClose,
  onWikilinkClick,
  className,
}: NoteSidePanelProps) {
  const { data, isLoading, isError } = useVaultNote(path);
  const [confirmOpen, setConfirmOpen] = useState(false);

  // iOS-safe scroll lock on mobile when panel is open (M4)
  // On desktop this panel is embedded in the page layout — no scroll lock needed.
  // We detect mobile via the hook always being active; the panel renders full-screen
  // on mobile (<sm) via conditional Tailwind classes below.
  useBodyScrollLock(!!path);

  // ── Edit mode ─────────────────────────────────────────────────────────────
  const [isEditing, setIsEditing] = useState(false);
  const [draftTitle, setDraftTitle] = useState("");
  const [draftBody, setDraftBody] = useState("");
  const [draftTags, setDraftTags] = useState("");
  const [editError, setEditError] = useState<string | null>(null);

  const queryClient = useQueryClient();
  const saveMutation = useMutation({
    mutationFn: (payload: { title: string; content: string; tags: string[] }) =>
      api.vault.update(path!, payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["vault"] });
      setIsEditing(false);
      setEditError(null);
    },
    onError: (err: Error) => {
      setEditError(err.message || "Speichern fehlgeschlagen");
    },
  });

  // Title priority: frontmatter.title > first H1 in body > readable path stem.
  // The Vault writer stores the canonical title in frontmatter, so most notes
  // skip the path-stem fallback entirely (which would otherwise leak UUIDs).
  const fm = data?.frontmatter ?? {};
  const fmTitle = typeof fm.title === "string" ? fm.title : null;
  const bodyH1 = data?.content
    ? (data.content.match(/^#\s+(.+?)\s*$/m)?.[1]?.trim() ?? null)
    : null;
  const title = fmTitle ?? bodyH1 ?? (path ? titleFromPath(path) : "");
  const breadcrumb = path ? breadcrumbFromPath(path) : "";

  // Derive agent from path convention: "agents/{slug}/..." → slug.
  // Frontmatter may also provide agent/type/tags overrides.
  const agentSlug =
    (fm.agent as string | undefined) ??
    (path?.startsWith("agents/") ? path.split("/")[1] ?? "" : "");
  const agentColor = colorForAgent(agentSlug);
  const noteType = (fm.type as string | undefined) ?? "";
  const rawTags = fm.tags ?? [];
  const tags = parseTags(
    Array.isArray(rawTags) ? rawTags : typeof rawTags === "string" ? rawTags : [],
  );

  // Date: frontmatter.date (canonical) → YYYY-MM-DD prefix in filename → null.
  const fmDate = typeof fm.date === "string" ? fm.date : null;
  let displayDate: string | null = null;
  if (fmDate) {
    const d = new Date(fmDate);
    if (!isNaN(d.getTime())) {
      displayDate = d
        .toLocaleDateString("en-US", {
          month: "long",
          day: "numeric",
          year: "numeric",
        })
        .toUpperCase();
    }
  }
  if (!displayDate && path) {
    const stem = path.split("/").pop() ?? "";
    const m = stem.match(/^(\d{4})-(\d{2})-(\d{2})/);
    if (m) {
      const months = ["JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY", "AUGUST", "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER"];
      const monthName = months[parseInt(m[2], 10) - 1] ?? m[2];
      displayDate = `${monthName} ${parseInt(m[3], 10)}, ${m[1]}`;
    }
  }

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
    if (!path) return;
    const cleanedTags = draftTags
      .split(/[,\n]/)
      .map((t) => t.trim().replace(/^#/, ""))
      .filter(Boolean);
    saveMutation.mutate({
      title: draftTitle.trim() || title,
      content: draftBody,
      tags: cleanedTags,
    });
  }, [path, draftTitle, draftBody, draftTags, title, saveMutation]);

  // Reset edit mode whenever the user switches notes.
  const lastPathRef = useRef<string | null>(null);
  useEffect(() => {
    if (lastPathRef.current !== path) {
      lastPathRef.current = path;
      setIsEditing(false);
      setEditError(null);
    }
  }, [path]);

  // ⌘S / Esc shortcuts while editing.
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
    <AnimatePresence>
      {path && (
        <motion.aside
          key={path}
          initial={{ x: "100%" }}
          animate={{ x: 0 }}
          exit={{ x: "100%" }}
          transition={{ type: "spring", stiffness: 200, damping: 26 }}
          className={`absolute right-0 top-0 bottom-0 z-20 flex flex-col overflow-hidden max-sm:!fixed max-sm:!inset-0 max-sm:!w-full ${className ?? ""}`}
          style={{
            width: "380px",
            background: "rgba(10,10,10,0.98)",
            borderLeft: "1px solid rgba(255,255,255,0.07)",
          }}
          aria-label="Note detail panel"
        >
          {/* ── Header ─────────────────────────────────────────────────────── */}
          <header
            className="shrink-0 px-5 py-4 flex flex-col gap-2"
            style={{ borderBottom: "1px solid rgba(255,255,255,0.05)", paddingTop: "calc(env(safe-area-inset-top) + 1rem)" }}
          >
            {/* Top row: breadcrumb + close button */}
            <div className="flex items-start justify-between gap-2">
              <div
                className="font-mono uppercase tracking-widest leading-tight"
                style={{ fontSize: "9px", color: "var(--color-text-muted)" }}
              >
                {breadcrumb || " "}
              </div>
              <div className="flex items-center gap-0.5 shrink-0">
                {isEditing ? (
                  <>
                    <button
                      type="button"
                      onClick={cancelEdit}
                      disabled={saveMutation.isPending}
                      className="rounded p-1 transition-colors"
                      style={{
                        color: "var(--color-text-muted)",
                        background: "transparent",
                        border: "none",
                        cursor: saveMutation.isPending ? "default" : "pointer",
                        opacity: saveMutation.isPending ? 0.4 : 1,
                      }}
                      aria-label="Cancel edit"
                      title="Abbrechen (Esc)"
                    >
                      <X style={{ width: "13px", height: "13px" }} />
                    </button>
                    <button
                      type="button"
                      onClick={handleSave}
                      disabled={saveMutation.isPending}
                      className="rounded p-1 transition-colors"
                      style={{
                        color: C.online,
                        background: "rgba(52,211,153,0.10)",
                        border: "1px solid rgba(52,211,153,0.30)",
                        cursor: saveMutation.isPending ? "default" : "pointer",
                      }}
                      aria-label="Save edit"
                      title="Speichern (⌘S)"
                    >
                      {saveMutation.isPending ? (
                        <span
                          className="inline-block rounded-full border-[1.5px] border-t-transparent animate-spin"
                          style={{
                            width: "11px",
                            height: "11px",
                            borderColor: C.online,
                            borderTopColor: "transparent",
                          }}
                        />
                      ) : (
                        <Check style={{ width: "12px", height: "12px" }} />
                      )}
                    </button>
                  </>
                ) : (
                  <>
                    <button
                      type="button"
                      onClick={startEdit}
                      className="rounded p-1 transition-colors"
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
                      aria-label="Edit note"
                      title="Bearbeiten"
                    >
                      <Pencil style={{ width: "12px", height: "12px" }} />
                    </button>
                    <button
                      type="button"
                      onClick={() => setConfirmOpen(true)}
                      className="rounded p-1 transition-colors"
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
                      aria-label="Delete note"
                      title="Note in Papierkorb verschieben"
                    >
                      <Trash2 style={{ width: "13px", height: "13px" }} />
                    </button>
                    <button
                      type="button"
                      onClick={onClose}
                      className="rounded p-1 transition-colors"
                      style={{
                        color: "var(--color-text-muted)",
                        background: "transparent",
                        border: "none",
                        cursor: "pointer",
                      }}
                      onMouseEnter={(e) => {
                        (e.currentTarget as HTMLButtonElement).style.color =
                          "var(--color-text-primary)";
                      }}
                      onMouseLeave={(e) => {
                        (e.currentTarget as HTMLButtonElement).style.color =
                          "var(--color-text-muted)";
                      }}
                      aria-label="Close note panel"
                    >
                      <X style={{ width: "14px", height: "14px" }} />
                    </button>
                  </>
                )}
              </div>
            </div>

            {/* Agent + type + date row */}
            <div className="flex items-center flex-wrap gap-x-2 gap-y-1">
              <span
                className="inline-block rounded-full shrink-0"
                style={{ width: "7px", height: "7px", background: agentColor }}
              />
              <span
                className="font-mono uppercase tracking-wider font-semibold"
                style={{ fontSize: "10px", color: "var(--color-text-secondary)" }}
              >
                {agentSlug || "global"}
              </span>
              {noteType && (
                <>
                  <span style={{ color: "rgba(255,255,255,0.15)", fontSize: "10px" }}>·</span>
                  <span
                    className="font-mono"
                    style={{ fontSize: "10px", color: "var(--color-text-muted)" }}
                  >
                    {noteType}
                  </span>
                </>
              )}
              {displayDate && (
                <>
                  <span style={{ color: "rgba(255,255,255,0.15)", fontSize: "10px" }}>·</span>
                  <span
                    className="font-mono uppercase tabular-nums"
                    style={{
                      fontSize: "10px",
                      letterSpacing: "0.12em",
                      color: "var(--color-text-muted)",
                    }}
                  >
                    {displayDate}
                  </span>
                </>
              )}
            </div>

            {/* Title — input when editing, h2 otherwise */}
            {isEditing ? (
              <input
                type="text"
                value={draftTitle}
                onChange={(e) => setDraftTitle(e.target.value)}
                placeholder="Titel"
                className="w-full bg-transparent outline-none"
                style={{
                  fontSize: "17px",
                  fontWeight: 700,
                  letterSpacing: "-0.01em",
                  lineHeight: 1.25,
                  color: "var(--color-text-primary)",
                  borderBottom: `1px dashed ${C.borderAccent}`,
                  paddingBottom: "3px",
                }}
                autoFocus
              />
            ) : (
              <h2
                className="font-bold text-lg tracking-tight leading-snug capitalize"
                style={{ color: "var(--color-text-primary)" }}
              >
                {title}
              </h2>
            )}

            {/* Tags — input when editing, chip row otherwise */}
            {isEditing ? (
              <div className="mt-1">
                <label
                  className="font-mono uppercase"
                  style={{
                    fontSize: "9px",
                    letterSpacing: "0.14em",
                    color: "var(--color-text-muted)",
                    display: "block",
                    marginBottom: "3px",
                  }}
                >
                  tags (komma)
                </label>
                <input
                  type="text"
                  value={draftTags}
                  onChange={(e) => setDraftTags(e.target.value)}
                  placeholder="tag1, tag2"
                  className="w-full bg-transparent outline-none font-mono"
                  style={{
                    fontSize: "11.5px",
                    color: "var(--color-text-secondary)",
                    borderBottom: `1px dashed ${C.border}`,
                    paddingBottom: "2px",
                  }}
                />
              </div>
            ) : (
              tags.length > 0 && (
                <div className="flex flex-wrap gap-1.5">
                  {tags.map((tag) => (
                    <span
                      key={tag}
                      className="font-mono rounded px-1.5 py-0.5"
                      style={{
                        fontSize: "10px",
                        background: "rgba(255,255,255,0.05)",
                        color: "var(--color-text-muted)",
                      }}
                    >
                      #{tag}
                    </span>
                  ))}
                </div>
              )
            )}

            {isEditing && editError && (
              <div
                className="mt-2 rounded-md px-2 py-1.5 flex items-center gap-1.5"
                style={{
                  background: "rgba(239,68,68,0.08)",
                  border: "1px solid rgba(239,68,68,0.25)",
                  fontSize: "11px",
                  color: STATUS_TEXT.error,
                }}
              >
                <X style={{ width: "11px", height: "11px" }} />
                {editError}
              </div>
            )}

            {/* Phase E "Verwandt"-Sektion — wenn die Note ein `task`-Field
                hat, zeigen wir alle anderen Notes desselben Tasks. Click
                routet via dem bestehenden onWikilinkClick (das in dieser
                Page-Variante schon Path-basiert ist). */}
            {!isEditing && typeof fm.task === "string" && path && (
              <RelatedNotesMini
                taskId={String(fm.task)}
                excludePath={path}
                onSelect={onWikilinkClick}
              />
            )}
          </header>

          {/* ── Content area ───────────────────────────────────────────────── */}
          <div className="flex-1 overflow-y-auto px-5 py-5">
            {isLoading && !isEditing && <SkeletonNote />}

            {isError && !isEditing && (
              <div
                className="text-sm font-mono"
                style={{ color: "var(--color-text-muted)" }}
              >
                Failed to load note content.
              </div>
            )}

            {isEditing ? (
              <textarea
                value={draftBody}
                onChange={(e) => setDraftBody(e.target.value)}
                placeholder="Markdown body"
                spellCheck={false}
                className="w-full h-full bg-transparent outline-none resize-none font-mono"
                style={{
                  fontSize: "12.5px",
                  lineHeight: 1.55,
                  color: "var(--color-text-body)",
                  minHeight: "55vh",
                  tabSize: 2,
                }}
                onKeyDown={(e) => {
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
              !isLoading && !isError && data && (
                <VaultMarkdown
                  content={data.content}
                  onWikilinkClick={onWikilinkClick}
                />
              )
            )}

            {!isLoading && !isError && !data && (
              <div
                className="text-sm font-mono"
                style={{ color: "var(--color-text-muted)" }}
              >
                No content available.
              </div>
            )}
          </div>
        </motion.aside>
      )}
      <ConfirmDeleteModal
        path={confirmOpen ? path : null}
        title={title}
        onClose={() => setConfirmOpen(false)}
        onDeleted={onClose}
      />
    </AnimatePresence>
  );
}
