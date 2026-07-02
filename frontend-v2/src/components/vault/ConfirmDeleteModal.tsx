"use client";

/**
 * ConfirmDeleteModal — gates vault note deletion with wikilink back-ref preview.
 *
 * Three-state lifecycle:
 *   1. Fetching back-refs   — disabled "Lädt…" button
 *   2. Confirm              — primary action ("In Papierkorb"), shows ref count
 *   3. Deleting             — spinner button, disabled cancel
 *
 * The button label is intentionally "In Papierkorb" (Trash), not "Löschen" —
 * the backend soft-deletes to ~/.mc/vault/_trash/ so the action is recoverable.
 * The operator can `mv ~/.mc/vault/_trash/<file> back` if needed.
 */

import { useEffect } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Trash2, X } from "lucide-react";
import { api } from "@/lib/api";
import { useBodyScrollLock } from "@/hooks/useBodyScrollLock";

interface ConfirmDeleteModalProps {
  /** Vault-relative path of the note. When null, the modal is closed. */
  path: string | null;
  /** Display title shown in the confirmation prompt (resolved by caller). */
  title: string;
  /** Called on successful delete OR on user cancel. */
  onClose: () => void;
  /** Optional: invoked after a successful delete (e.g. clear selection). */
  onDeleted?: () => void;
}

export function ConfirmDeleteModal({
  path,
  title,
  onClose,
  onDeleted,
}: ConfirmDeleteModalProps) {
  const queryClient = useQueryClient();
  const open = path != null;

  // iOS-safe scroll lock (M4) — small confirm, stays centered per M12
  useBodyScrollLock(open);

  // Pre-fetch back-refs while the user reads the prompt — gives them an
  // honest "X other notes link to this" count before they confirm.
  const { data: backrefData, isLoading: refsLoading } = useQuery({
    queryKey: ["vault", "backrefs", path],
    queryFn: () => api.vault.backrefs(path!),
    enabled: open,
    staleTime: 30_000,
  });

  const deleteMutation = useMutation({
    mutationFn: (p: string) => api.vault.delete(p),
    onSuccess: () => {
      // Nuke every vault query — list + search + graph + backrefs.
      queryClient.invalidateQueries({ queryKey: ["vault"] });
      onDeleted?.();
      onClose();
    },
  });

  // Esc to close (only when not mid-delete — abort-mid-flight is messy).
  useEffect(() => {
    if (!open) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !deleteMutation.isPending) onClose();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [open, onClose, deleteMutation.isPending]);

  const backrefs = backrefData?.backrefs ?? [];
  const refCount = backrefs.length;

  return (
    <AnimatePresence>
      {open && (
        <motion.div
          key="confirm-delete-overlay"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.12 }}
          className="fixed inset-0 z-[100] flex items-center justify-center px-4"
          style={{ background: "rgba(0,0,0,0.62)", backdropFilter: "blur(4px)" }}
          onClick={() => !deleteMutation.isPending && onClose()}
        >
          <motion.div
            initial={{ opacity: 0, y: 12, scale: 0.97 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: 8, scale: 0.98 }}
            transition={{ type: "spring", stiffness: 300, damping: 26 }}
            className="w-full max-w-md rounded-xl flex flex-col"
            style={{
              background: "rgba(15,15,15,0.98)",
              border: "1px solid rgba(255,255,255,0.08)",
              boxShadow: "0 24px 80px rgba(0,0,0,0.6), 0 1px 0 rgba(255,255,255,0.04) inset",
            }}
            onClick={(e) => e.stopPropagation()}
            role="dialog"
            aria-modal="true"
            aria-label="Confirm note deletion"
          >
            {/* Header */}
            <div
              className="flex items-start justify-between gap-3 px-5 pt-5 pb-3"
              style={{ borderBottom: "1px solid rgba(255,255,255,0.04)" }}
            >
              <div className="flex items-center gap-3 min-w-0">
                <div
                  className="shrink-0 w-9 h-9 rounded-full flex items-center justify-center"
                  style={{
                    background: "rgba(239,68,68,0.10)",
                    border: "1px solid rgba(239,68,68,0.25)",
                  }}
                >
                  <Trash2 size={16} style={{ color: "#fca5a5" }} />
                </div>
                <div className="min-w-0">
                  <div
                    className="font-mono uppercase font-semibold"
                    style={{
                      fontSize: "10px",
                      letterSpacing: "0.18em",
                      color: "var(--color-text-muted)",
                    }}
                  >
                    Note in Papierkorb verschieben
                  </div>
                  <div
                    className="truncate"
                    style={{
                      fontSize: "15.5px",
                      fontWeight: 600,
                      color: "var(--color-text-primary)",
                      letterSpacing: "-0.005em",
                    }}
                    title={title}
                  >
                    {title}
                  </div>
                </div>
              </div>
              <button
                type="button"
                onClick={onClose}
                disabled={deleteMutation.isPending}
                aria-label="Abbrechen"
                className="shrink-0 rounded p-1"
                style={{
                  color: "var(--color-text-muted)",
                  background: "transparent",
                  border: "none",
                  cursor: deleteMutation.isPending ? "default" : "pointer",
                  opacity: deleteMutation.isPending ? 0.4 : 1,
                }}
              >
                <X size={14} />
              </button>
            </div>

            {/* Body */}
            <div className="px-5 py-4 space-y-3">
              <p
                style={{
                  fontSize: "13.5px",
                  lineHeight: 1.55,
                  color: "var(--color-text-secondary)",
                }}
              >
                Die Datei wird in den Vault-Papierkorb verschoben
                (<code
                  className="font-mono"
                  style={{
                    fontSize: "11.5px",
                    background: "rgba(255,255,255,0.05)",
                    padding: "1px 4px",
                    borderRadius: "3px",
                    color: "var(--color-text-muted)",
                  }}
                >
                  ~/.mc/vault/_trash/
                </code>).
                Search-Index und Graph aktualisieren sich automatisch. Du kannst
                die Datei manuell zurückverschieben, falls du sie doch brauchst.
              </p>

              {/* Back-refs warning */}
              {refsLoading ? (
                <div
                  className="font-mono"
                  style={{
                    fontSize: "11px",
                    letterSpacing: "0.06em",
                    color: "var(--color-text-muted)",
                  }}
                >
                  Prüfe Wikilinks…
                </div>
              ) : refCount > 0 ? (
                <div
                  className="rounded-md px-3 py-2.5 flex gap-2.5 items-start"
                  style={{
                    background: "rgba(251,191,36,0.06)",
                    border: "1px solid rgba(251,191,36,0.22)",
                  }}
                >
                  <AlertTriangle
                    size={14}
                    style={{ color: "#fbbf24", marginTop: "1px", flexShrink: 0 }}
                  />
                  <div className="min-w-0">
                    <div
                      style={{
                        fontSize: "12.5px",
                        fontWeight: 600,
                        color: "#fbbf24",
                        marginBottom: "3px",
                      }}
                    >
                      {refCount === 1
                        ? "1 andere Note verlinkt hierhin"
                        : `${refCount} andere Notes verlinken hierhin`}
                    </div>
                    <ul
                      className="space-y-0.5"
                      style={{ fontSize: "11.5px", color: "var(--color-text-secondary)" }}
                    >
                      {backrefs.slice(0, 4).map((r) => (
                        <li
                          key={r.path}
                          className="font-mono truncate"
                          title={r.path}
                        >
                          <span style={{ opacity: 0.55 }}>{r.agent ?? "—"} ·</span>{" "}
                          {r.title || r.path}
                        </li>
                      ))}
                      {refCount > 4 && (
                        <li
                          className="font-mono"
                          style={{ color: "var(--color-text-muted)", opacity: 0.7 }}
                        >
                          + {refCount - 4} weitere
                        </li>
                      )}
                    </ul>
                    <div
                      style={{
                        fontSize: "11px",
                        color: "var(--color-text-muted)",
                        marginTop: "6px",
                      }}
                    >
                      Diese Wikilinks zeigen nach dem Löschen ins Leere.
                    </div>
                  </div>
                </div>
              ) : (
                <div
                  className="font-mono"
                  style={{
                    fontSize: "11px",
                    letterSpacing: "0.06em",
                    color: "var(--color-text-muted)",
                  }}
                >
                  Keine Wikilinks auf diese Note.
                </div>
              )}

              {/* Error */}
              {deleteMutation.isError && (
                <div
                  className="rounded-md px-3 py-2"
                  style={{
                    background: "rgba(239,68,68,0.08)",
                    border: "1px solid rgba(239,68,68,0.25)",
                    fontSize: "12px",
                    color: "#fca5a5",
                  }}
                >
                  Löschen fehlgeschlagen:{" "}
                  {(deleteMutation.error as Error)?.message ?? "Unbekannter Fehler"}
                </div>
              )}
            </div>

            {/* Footer */}
            <div
              className="flex items-center justify-end gap-2 px-5 py-3"
              style={{ borderTop: "1px solid rgba(255,255,255,0.04)" }}
            >
              <button
                type="button"
                onClick={onClose}
                disabled={deleteMutation.isPending}
                className="font-mono uppercase rounded-md px-3 py-2 transition-colors"
                style={{
                  fontSize: "10.5px",
                  letterSpacing: "0.14em",
                  background: "transparent",
                  border: "1px solid rgba(255,255,255,0.08)",
                  color: "var(--color-text-secondary)",
                  cursor: deleteMutation.isPending ? "default" : "pointer",
                  opacity: deleteMutation.isPending ? 0.5 : 1,
                }}
              >
                Abbrechen
              </button>
              <button
                type="button"
                onClick={() => path && deleteMutation.mutate(path)}
                disabled={deleteMutation.isPending || !path}
                className="font-mono uppercase rounded-md px-3 py-2 transition-colors flex items-center gap-1.5"
                style={{
                  fontSize: "10.5px",
                  letterSpacing: "0.14em",
                  background: deleteMutation.isPending
                    ? "rgba(239,68,68,0.18)"
                    : "rgba(239,68,68,0.14)",
                  border: "1px solid rgba(239,68,68,0.45)",
                  color: "#fca5a5",
                  cursor: deleteMutation.isPending ? "default" : "pointer",
                }}
              >
                {deleteMutation.isPending ? (
                  <>
                    <span
                      className="inline-block w-3 h-3 rounded-full border-[1.5px] border-t-transparent animate-spin"
                      style={{ borderColor: "#fca5a5", borderTopColor: "transparent" }}
                    />
                    Löschen…
                  </>
                ) : (
                  <>
                    <Trash2 size={12} />
                    In Papierkorb
                  </>
                )}
              </button>
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
