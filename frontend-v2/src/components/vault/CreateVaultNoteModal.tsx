"use client";

/**
 * CreateVaultNoteModal — "Neuer Eintrag" für die memory-Page.
 *
 * Modelliert nach CreateTaskModal (CreateTaskModal.tsx) — gleiche Modal-
 * Choreografie (overlay, focus trap, ESC + Cmd+Enter shortcuts, mobile
 * bottom-sheet auf <sm) aber radikal schlankere Felder weil ein Vault-
 * Eintrag aus Title + Body + Type + Tags besteht. Kein Board, kein Agent
 * Picker (Default-Namespace agents/mark/ — wie mit dem Operator besprochen).
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { motion, AnimatePresence, useReducedMotion } from "framer-motion";
import { X, Send, Plus, Hash } from "lucide-react";
import { useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { notify } from "@/lib/notify";
import type { VaultNoteType } from "@/lib/types";
import { C as _C } from "@/lib/colors";
import { useBodyScrollLock } from "@/hooks/useBodyScrollLock";

// Convenience aliases mapping the local modal token names to lib/colors.
const C = {
  deep:        _C.bgDeep,
  elevated:    _C.bgElevated,
  border:      _C.border,
  borderSubtle:_C.borderSubtle,
  accent:      _C.accent,
  error:       _C.error,
  textPrimary: _C.textPrimary,
  textMuted:   _C.textMuted,
  inputBg:     "rgba(255,255,255,0.02)",
};

// Auswählbare Note-Types. Reihenfolge = Häufigkeit der Use-Cases des Operators:
// note (default, schnelle Gedanken), knowledge (etwas Gelerntes festhalten),
// journal (Tageseintrag), lesson/reference für späteres Detail-Tagging.
// Werte 1:1 wie im Backend (_ADMIN_NOTE_TYPES).
const NOTE_TYPES: { value: VaultNoteType; label: string; hint: string }[] = [
  { value: "note",       label: "Note",       hint: "freier Eintrag" },
  { value: "knowledge",  label: "Knowledge",  hint: "fakten / wissen" },
  { value: "journal",    label: "Journal",    hint: "tagesnotiz" },
  { value: "lesson",     label: "Lesson",     hint: "lernerfahrung" },
  { value: "reference",  label: "Reference",  hint: "link / quelle" },
];

interface CreateVaultNoteModalProps {
  /** Wenn null → Trigger-Button ist disabled (Konsistenz mit CreateTaskModal). */
  enabled: boolean;
  /** Wird mit dem neuen Vault-Path aufgerufen wenn der Save erfolgreich war —
   *  damit der Caller den Eintrag direkt selektieren kann. */
  onCreated?: (path: string) => void;
}

export function CreateVaultNoteModal({ enabled, onCreated }: CreateVaultNoteModalProps) {
  const qc = useQueryClient();

  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);

  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [type, setType] = useState<VaultNoteType>("note");
  const [tagsRaw, setTagsRaw] = useState("");

  const dialogRef = useRef<HTMLDivElement>(null);
  const previouslyFocused = useRef<HTMLElement | null>(null);
  const titleRef = useRef<HTMLInputElement>(null);
  const bodyRef = useRef<HTMLTextAreaElement>(null);
  const prefersReducedMotion = useReducedMotion();

  // Title min-3, Body min-1 — match the backend Field(min_length=…) so
  // disabled state on the submit button mirrors the server-side validation.
  const titleTrimmed = title.trim();
  const contentTrimmed = content.trim();
  const canSubmit = titleTrimmed.length >= 3 && contentTrimmed.length >= 1;

  const tags = useMemo(() => {
    // Whitespace + Komma trennen, '#' am Anfang dulden, leere Tokens raus,
    // dedupe. Identische Logik zum Edit-Panel.
    const seen = new Set<string>();
    const out: string[] = [];
    for (const raw of tagsRaw.split(/[\s,]+/)) {
      const t = raw.trim().replace(/^#/, "");
      if (t && !seen.has(t)) {
        seen.add(t);
        out.push(t);
      }
    }
    return out;
  }, [tagsRaw]);

  const resetForm = useCallback(() => {
    setTitle("");
    setContent("");
    setType("note");
    setTagsRaw("");
    setOpen(false);
  }, []);

  // Auto-focus Title beim Öffnen + Save previously-focused so wir später
  // dorthin zurückspringen. Selbe Choreografie wie CreateTaskModal.
  useEffect(() => {
    if (!open) return;
    previouslyFocused.current = document.activeElement as HTMLElement | null;
    const t = setTimeout(() => titleRef.current?.focus(), 80);
    return () => {
      clearTimeout(t);
      previouslyFocused.current?.focus?.();
    };
  }, [open]);

  // Body-Textarea bei Open einmal grosszügig vorbestimmen — verhindert
  // dass ein leerer Zustand wie 1-zeiliges Input wirkt.
  useEffect(() => {
    if (!open) return;
    const t = setTimeout(() => {
      const el = bodyRef.current;
      if (el) {
        el.style.height = "auto";
        el.style.height = `${Math.max(el.scrollHeight, 160)}px`;
      }
    }, 10);
    return () => clearTimeout(t);
  }, [open]);

  // Focus-Trap — Tab + Shift-Tab innerhalb des Dialogs halten.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Tab") return;
      const root = dialogRef.current;
      if (!root) return;
      const focusables = root.querySelectorAll<HTMLElement>(
        'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])'
      );
      if (focusables.length === 0) return;
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  // iOS-safe scroll lock (M4)
  useBodyScrollLock(open);

  const handleSubmit = useCallback(async () => {
    if (!canSubmit || loading) return;
    setLoading(true);
    try {
      const res = await api.vault.create({
        title: titleTrimmed,
        content: contentTrimmed,
        type,
        tags,
      });
      // Invalidate alles was die memory-Page neu laden muss: liste, suche,
      // graph, trash-counter etc. Ein einziger Prefix-Invalidate reicht
      // weil TanStack-Query partial-matching macht.
      qc.invalidateQueries({ queryKey: ["vault"] });
      notify.success("Eintrag erstellt");
      onCreated?.(res.path);
      resetForm();
    } catch (err) {
      const msg = err instanceof Error && err.message ? err.message : "Fehler beim Erstellen";
      notify.error(msg);
    } finally {
      setLoading(false);
    }
  }, [canSubmit, loading, titleTrimmed, contentTrimmed, type, tags, qc, onCreated, resetForm]);

  return (
    <>
      {/* Trigger */}
      <button
        onClick={() => setOpen(true)}
        disabled={!enabled}
        className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-[11px] font-medium transition-all cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed"
        style={{
          color: C.accent,
          border: `1px solid ${C.accent}44`,
          backgroundColor: `${C.accent}0A`,
        }}
      >
        <Plus size={12} />
        Neuer Eintrag
      </button>

      <AnimatePresence>
        {open && (
          <motion.div
            initial={prefersReducedMotion ? false : { opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={prefersReducedMotion ? { opacity: 1 } : { opacity: 0 }}
            transition={{ duration: prefersReducedMotion ? 0 : 0.15 }}
            className="fixed inset-0 z-50 flex items-end sm:items-center justify-center sm:p-4"
            style={{ paddingBottom: "env(safe-area-inset-bottom)" }}
            onClick={(e) => { if (e.target === e.currentTarget) resetForm(); }}
            onKeyDown={(e) => {
              if (e.key === "Escape") {
                e.preventDefault();
                resetForm();
              } else if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                e.preventDefault();
                handleSubmit();
              }
            }}
          >
            {/* Backdrop */}
            <div
              className="absolute inset-0"
              style={{ backgroundColor: "rgba(0,0,0,0.6)" }}
            />

            {/* Drag indicator — mobile bottom-sheet hint */}
            <div className="sm:hidden absolute bottom-[calc(92dvh-0.5rem)] left-1/2 -translate-x-1/2 z-10 w-8 h-1 rounded-full" style={{ backgroundColor: "rgba(255,255,255,0.2)" }} />

            <motion.div
              ref={dialogRef}
              role="dialog"
              aria-modal="true"
              aria-labelledby="create-vault-note-title"
              initial={prefersReducedMotion ? false : { opacity: 0, y: 24 }}
              animate={{ opacity: 1, y: 0 }}
              exit={prefersReducedMotion ? { opacity: 1 } : { opacity: 0, y: 24 }}
              transition={{ duration: prefersReducedMotion ? 0 : 0.22, ease: [0.16, 1, 0.3, 1] }}
              className="relative w-full mx-2 rounded-t-2xl rounded-b-none sm:mx-0 sm:max-w-[680px] sm:rounded-2xl overflow-hidden max-h-[92dvh] sm:max-h-[88vh] flex flex-col"
              style={{
                background: C.elevated,
                border: "1px solid rgba(255,255,255,0.08)",
                boxShadow: "0 4px 24px rgba(0,0,0,0.5), 0 1px 2px rgba(0,0,0,0.3)",
              }}
            >
              {/* Top edge highlight */}
              <div className="absolute top-0 left-0 right-0 h-px" style={{ background: "linear-gradient(90deg, transparent, rgba(255,255,255,0.12), transparent)" }} />

              {/* Header */}
              <div className="flex items-center justify-between px-5 py-3.5 shrink-0" style={{ borderBottom: `1px solid ${C.borderSubtle}` }}>
                <span id="create-vault-note-title" className="text-sm font-semibold" style={{ color: C.textPrimary }}>
                  Neuer Eintrag
                </span>
                <button onClick={resetForm} className="cursor-pointer hover:opacity-80 transition-opacity" style={{ color: C.textMuted }}>
                  <X size={16} />
                </button>
              </div>

              {/* Body */}
              <div className="p-5 overflow-y-auto flex-1 space-y-4">
                {/* Type pills — same row, same metaphor as CreateTaskModal's
                    template chips so the muscle memory transfers. */}
                <div>
                  <label className="text-[10px] uppercase tracking-wider font-mono block mb-2" style={{ color: C.textMuted }}>
                    Typ
                  </label>
                  <div className="flex flex-wrap gap-1.5">
                    {NOTE_TYPES.map((t) => {
                      const active = type === t.value;
                      return (
                        <button
                          key={t.value}
                          type="button"
                          onClick={() => setType(t.value)}
                          className="px-2.5 py-1 rounded-md text-[11px] font-mono transition-all cursor-pointer"
                          style={{
                            color: active ? C.accent : C.textMuted,
                            background: active ? `${C.accent}15` : "transparent",
                            border: `1px solid ${active ? `${C.accent}55` : C.border}`,
                          }}
                          title={t.hint}
                        >
                          {t.label}
                        </button>
                      );
                    })}
                  </div>
                </div>

                {/* Title */}
                <div>
                  <label className="text-[10px] uppercase tracking-wider font-mono block mb-2" style={{ color: C.textMuted }}>
                    Titel
                  </label>
                  <input
                    ref={titleRef}
                    type="text"
                    value={title}
                    onChange={(e) => setTitle(e.target.value)}
                    placeholder="Was willst du dir merken?"
                    spellCheck={true}
                    disabled={loading}
                    className="w-full px-3 py-2 rounded-md text-sm outline-none transition-colors"
                    style={{
                      background: C.inputBg,
                      border: `1px solid ${C.border}`,
                      color: C.textPrimary,
                    }}
                  />
                </div>

                {/* Body */}
                <div>
                  <label className="text-[10px] uppercase tracking-wider font-mono block mb-2" style={{ color: C.textMuted }}>
                    Inhalt
                  </label>
                  <textarea
                    ref={bodyRef}
                    value={content}
                    onChange={(e) => {
                      setContent(e.target.value);
                      // Auto-grow auf Inhalt — caps bei ~50dvh damit der Footer
                      // nicht aus dem Viewport rutscht.
                      const el = e.currentTarget;
                      el.style.height = "auto";
                      el.style.height = `${Math.min(el.scrollHeight, window.innerHeight * 0.5)}px`;
                    }}
                    placeholder="Markdown erlaubt. Wikilinks per [[note-slug]]."
                    spellCheck={true}
                    disabled={loading}
                    className="w-full px-3 py-2 rounded-md text-sm outline-none transition-colors font-mono resize-none"
                    style={{
                      background: C.inputBg,
                      border: `1px solid ${C.border}`,
                      color: C.textPrimary,
                      minHeight: "160px",
                      lineHeight: 1.55,
                      tabSize: 2,
                    }}
                    onKeyDown={(e) => {
                      // Tab inserts 2 spaces — matches the edit-panel
                      // behavior, default Tab is hostile to markdown lists.
                      if (e.key === "Tab" && !e.shiftKey) {
                        e.preventDefault();
                        const t = e.currentTarget;
                        const start = t.selectionStart;
                        const end = t.selectionEnd;
                        const next = t.value.slice(0, start) + "  " + t.value.slice(end);
                        setContent(next);
                        requestAnimationFrame(() => {
                          t.selectionStart = t.selectionEnd = start + 2;
                        });
                      }
                    }}
                  />
                </div>

                {/* Tags */}
                <div>
                  <label className="text-[10px] uppercase tracking-wider font-mono mb-2 flex items-center gap-1.5" style={{ color: C.textMuted }}>
                    <Hash size={10} /> Tags <span className="opacity-60">(optional, komma- oder leertrennt)</span>
                  </label>
                  <input
                    type="text"
                    value={tagsRaw}
                    onChange={(e) => setTagsRaw(e.target.value)}
                    placeholder="z.b.  personal, idee, todo"
                    disabled={loading}
                    className="w-full px-3 py-2 rounded-md text-sm outline-none transition-colors"
                    style={{
                      background: C.inputBg,
                      border: `1px solid ${C.border}`,
                      color: C.textPrimary,
                    }}
                  />
                  {tags.length > 0 && (
                    <div className="flex flex-wrap gap-1 mt-2">
                      {tags.map((t) => (
                        <span
                          key={t}
                          className="px-1.5 py-0.5 rounded text-[10px] font-mono"
                          style={{
                            color: C.accent,
                            background: `${C.accent}12`,
                            border: `1px solid ${C.accent}33`,
                          }}
                        >
                          {t}
                        </span>
                      ))}
                    </div>
                  )}
                </div>

                {/* Hint: target path so the operator sees where it lands. Helps build
                    intuition for the vault topology before he goes hunting in
                    the Liste. */}
                <div
                  className="text-[10px] font-mono px-3 py-2 rounded-md"
                  style={{
                    color: C.textMuted,
                    background: "rgba(255,255,255,0.015)",
                    border: `1px dashed ${C.border}`,
                  }}
                >
                  → agents/mark/{type}s/...
                </div>
              </div>

              {/* Footer */}
              <div className="flex items-center justify-between px-5 py-3.5 shrink-0" style={{ borderTop: `1px solid ${C.borderSubtle}` }}>
                <span className="text-[10px]" style={{ color: C.textMuted }}>
                  Cmd+Enter = speichern · Esc = schliessen
                </span>
                <div className="flex items-center gap-2">
                  <button
                    type="button"
                    onClick={resetForm}
                    className="px-3.5 py-1.5 text-[11px] rounded-lg cursor-pointer transition-colors"
                    style={{ color: C.textMuted, border: `1px solid ${C.border}` }}
                  >
                    Abbrechen
                  </button>
                  <button
                    type="button"
                    onClick={handleSubmit}
                    disabled={!canSubmit || loading}
                    className="flex items-center gap-1.5 px-3.5 py-1.5 text-[11px] font-semibold rounded-lg cursor-pointer transition-all disabled:opacity-30 disabled:cursor-not-allowed"
                    style={{
                      background: `linear-gradient(135deg, ${C.accent}, ${_C.accentHover})`,
                      color: _C.bgDeep,
                    }}
                  >
                    <Send size={11} />
                    {loading ? "..." : "Eintrag erstellen"}
                  </button>
                </div>
              </div>
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>
    </>
  );
}
