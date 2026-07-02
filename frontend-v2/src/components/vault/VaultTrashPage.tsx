"use client";

/**
 * VaultTrashPage — Papierkorb tab inside /memory.
 *
 * Lists everything sitting in ~/.mc/vault/_trash/, lets the admin either
 * restore (move back to original path) or permanently purge.
 *
 * Layout mirrors the Editorial Codex list (marginalia date + content +
 * actions column) so the visual language stays consistent across tabs.
 *
 * Edge cases the UI surfaces:
 *   - Original path occupied → backend returns 409, error toast shown.
 *   - Legacy/manual trash files without MC prefix → restore button disabled,
 *     tooltip explains "restore manually via mv".
 *   - Purge requires modal confirmation (genuinely destructive).
 *   - Live updates: vault:stream `restored` / `trash_purged` events
 *     invalidate the trash query automatically.
 */

import { useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { RotateCcw, Trash2, X, AlertTriangle, ArchiveX } from "lucide-react";
import { api } from "@/lib/api";
import { colorForAgent } from "./agentColors";
import { C, STATUS_TEXT } from "@/lib/colors";

// ── Helpers ────────────────────────────────────────────────────────────────────

const MONTHS_SHORT = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"] as const;

function parseTrashDate(iso: string | null): { day: string; month: string; year: string; time: string } | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (isNaN(d.getTime())) return null;
  return {
    day: String(d.getDate()),
    month: MONTHS_SHORT[d.getMonth()] ?? "",
    year: String(d.getFullYear()),
    time: `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`,
  };
}

function humanBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

// ── Purge confirmation modal ──────────────────────────────────────────────────

function PurgeConfirmModal({
  filename,
  title,
  onClose,
  onConfirm,
  isPurging,
  error,
}: {
  filename: string | null;
  title: string;
  onClose: () => void;
  onConfirm: () => void;
  isPurging: boolean;
  error: string | null;
}) {
  return (
    <AnimatePresence>
      {filename && (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.12 }}
          className="fixed inset-0 z-[100] flex items-center justify-center px-4"
          style={{ background: "rgba(0,0,0,0.62)", backdropFilter: "blur(4px)" }}
          onClick={() => !isPurging && onClose()}
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
              boxShadow: "0 24px 80px rgba(0,0,0,0.6)",
            }}
            onClick={(e) => e.stopPropagation()}
            role="dialog"
            aria-modal="true"
          >
            <div className="flex items-start gap-3 px-5 pt-5 pb-3">
              <div
                className="shrink-0 w-9 h-9 rounded-full flex items-center justify-center"
                style={{
                  background: `${C.error}1A`,
                  border: `1px solid ${C.error}40`,
                }}
              >
                <ArchiveX size={16} style={{ color: STATUS_TEXT.error }} />
              </div>
              <div className="min-w-0 flex-1">
                <div
                  className="font-mono uppercase font-semibold"
                  style={{
                    fontSize: "10px",
                    letterSpacing: "0.18em",
                    color: "var(--color-text-muted)",
                  }}
                >
                  Permanent löschen
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
                  {title || filename}
                </div>
              </div>
            </div>

            <div className="px-5 py-3">
              <div
                className="rounded-md px-3 py-2.5 flex gap-2.5 items-start"
                style={{
                  background: `${C.error}0F`,
                  border: `1px solid ${C.error}38`,
                }}
              >
                <AlertTriangle
                  size={14}
                  style={{ color: STATUS_TEXT.error, marginTop: "1px", flexShrink: 0 }}
                />
                <div style={{ fontSize: "12.5px", color: "var(--color-text-secondary)" }}>
                  Diese Datei wird endgültig aus dem Papierkorb entfernt. Keine
                  Wiederherstellung mehr möglich (ausser via Datenbank-Backup).
                </div>
              </div>
              {error && (
                <div
                  className="rounded-md px-3 py-2 mt-2"
                  style={{
                    background: `${C.error}14`,
                    border: `1px solid ${C.error}40`,
                    fontSize: "12px",
                    color: STATUS_TEXT.error,
                  }}
                >
                  {error}
                </div>
              )}
            </div>

            <div
              className="flex items-center justify-end gap-2 px-5 py-3"
              style={{ borderTop: "1px solid rgba(255,255,255,0.04)" }}
            >
              <button
                onClick={onClose}
                disabled={isPurging}
                className="font-mono uppercase rounded-md px-3 py-2"
                style={{
                  fontSize: "10.5px",
                  letterSpacing: "0.14em",
                  background: "transparent",
                  border: "1px solid rgba(255,255,255,0.08)",
                  color: "var(--color-text-secondary)",
                  cursor: isPurging ? "default" : "pointer",
                  opacity: isPurging ? 0.5 : 1,
                }}
              >
                Abbrechen
              </button>
              <button
                onClick={onConfirm}
                disabled={isPurging}
                className="font-mono uppercase rounded-md px-3 py-2 flex items-center gap-1.5"
                style={{
                  fontSize: "10.5px",
                  letterSpacing: "0.14em",
                  background: `${C.error}24`,
                  border: `1px solid ${C.error}73`,
                  color: STATUS_TEXT.error,
                  cursor: isPurging ? "default" : "pointer",
                }}
              >
                {isPurging ? (
                  <>
                    <span
                      className="inline-block w-3 h-3 rounded-full border-[1.5px] border-t-transparent animate-spin"
                      style={{ borderColor: STATUS_TEXT.error, borderTopColor: "transparent" }}
                    />
                    Löschen…
                  </>
                ) : (
                  <>
                    <Trash2 size={12} />
                    Permanent löschen
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

// ── Trash row ─────────────────────────────────────────────────────────────────

interface TrashItem {
  trash_filename: string;
  original_path: string | null;
  trashed_at: string | null;
  title: string;
  agent: string;
  type: string;
  tags: string[];
  date: string;
  size_bytes: number;
}

function TrashRow({
  item,
  onRestore,
  onPurge,
  restoreError,
  restorePending,
  restorePendingFor,
}: {
  item: TrashItem;
  onRestore: (filename: string) => void;
  onPurge: (filename: string, title: string) => void;
  restoreError: string | null;
  restorePending: boolean;
  restorePendingFor: string | null;
}) {
  const trashedAt = parseTrashDate(item.trashed_at);
  const agentColor = colorForAgent(item.agent);
  const restorable = item.original_path != null;
  const isRestoring = restorePending && restorePendingFor === item.trash_filename;

  return (
    <div
      className="flex relative"
      style={{
        background: "transparent",
        borderBottom: "1px dashed rgba(255,255,255,0.04)",
      }}
    >
      {/* Marginalia — date trashed */}
      <div className="shrink-0 w-[92px] py-5 pl-3 pr-4 text-right select-none">
        {trashedAt ? (
          <>
            <div
              className="text-[28px] font-bold leading-none tracking-tighter tabular-nums"
              style={{ color: "var(--color-text-secondary)", opacity: 0.65 }}
            >
              {trashedAt.day}
            </div>
            <div
              className="font-mono uppercase font-semibold mt-1.5"
              style={{
                fontSize: "10px",
                letterSpacing: "0.18em",
                color: "var(--color-text-muted)",
              }}
            >
              {trashedAt.month}
            </div>
            <div
              className="font-mono tabular-nums mt-0.5"
              style={{
                fontSize: "9.5px",
                color: "rgba(255,255,255,0.22)",
              }}
            >
              {trashedAt.year}
            </div>
            <div
              className="font-mono tabular-nums mt-0.5"
              style={{
                fontSize: "9px",
                color: "rgba(255,255,255,0.18)",
              }}
            >
              {trashedAt.time}
            </div>
          </>
        ) : (
          <div
            className="font-mono mt-2"
            style={{ fontSize: "22px", color: "rgba(255,255,255,0.14)" }}
          >
            ◌
          </div>
        )}
      </div>

      {/* Vertical rule */}
      <div
        className="shrink-0 w-px self-stretch"
        style={{
          background:
            "linear-gradient(to bottom, transparent 0%, rgba(255,255,255,0.08) 18%, rgba(255,255,255,0.08) 82%, transparent 100%)",
        }}
      />

      {/* Content */}
      <div className="flex-1 min-w-0 py-5 px-5">
        <div className="flex items-center gap-2.5 mb-2">
          {item.type && (
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
                opacity: 0.7,
              }}
            >
              {item.type}
            </span>
          )}
          {item.agent && (
            <span
              className="font-mono lowercase"
              style={{
                fontSize: "10px",
                letterSpacing: "0.04em",
                color: "var(--color-text-muted)",
              }}
            >
              {item.agent}
            </span>
          )}
          <span
            className="font-mono"
            style={{
              fontSize: "10px",
              color: "rgba(255,255,255,0.2)",
              marginLeft: "auto",
            }}
          >
            {humanBytes(item.size_bytes)}
          </span>
        </div>

        <div
          className="font-semibold leading-snug mb-1.5"
          style={{
            fontSize: "15.5px",
            letterSpacing: "-0.005em",
            color: "var(--color-text-secondary)",
            textDecoration: "line-through",
            textDecorationColor: "rgba(255,255,255,0.18)",
            textDecorationThickness: "1px",
          }}
        >
          {item.title || item.original_path || item.trash_filename}
        </div>

        {item.original_path && (
          <div
            className="font-mono truncate"
            style={{
              fontSize: "10.5px",
              color: "var(--color-text-muted)",
              opacity: 0.6,
            }}
            title={item.original_path}
          >
            ← {item.original_path}
          </div>
        )}

        {item.tags.length > 0 && (
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 mt-2.5">
            {item.tags.slice(0, 5).map((tag) => (
              <span
                key={tag}
                className="font-mono"
                style={{
                  fontSize: "10.5px",
                  color: "var(--color-text-muted)",
                  opacity: 0.55,
                }}
              >
                #{tag}
              </span>
            ))}
          </div>
        )}

        {restoreError && (
          <div
            className="mt-3 rounded-md px-2.5 py-1.5 inline-flex items-center gap-2"
            style={{
              background: `${C.error}0F`,
              border: `1px solid ${C.error}33`,
              fontSize: "11.5px",
              color: STATUS_TEXT.error,
            }}
          >
            <AlertTriangle size={11} />
            {restoreError}
          </div>
        )}
      </div>

      {/* Actions */}
      <div className="shrink-0 flex items-center gap-1 px-3 self-stretch">
        <button
          type="button"
          onClick={() => restorable && onRestore(item.trash_filename)}
          disabled={!restorable || isRestoring}
          aria-label="Restore note"
          title={
            restorable
              ? "Note wiederherstellen"
              : "Original-Pfad unbekannt (Legacy-Trash) — manuell wiederherstellen"
          }
          className="rounded-md p-2 transition-colors"
          style={{
            color: restorable ? "#34d399" : "rgba(255,255,255,0.18)",
            background: "transparent",
            border: "1px solid",
            borderColor: restorable ? "rgba(52,211,153,0.25)" : "rgba(255,255,255,0.04)",
            cursor: restorable && !isRestoring ? "pointer" : "default",
            opacity: isRestoring ? 0.5 : 1,
          }}
          onMouseEnter={(e) => {
            if (restorable && !isRestoring) {
              (e.currentTarget as HTMLButtonElement).style.background =
                "rgba(52,211,153,0.08)";
            }
          }}
          onMouseLeave={(e) => {
            (e.currentTarget as HTMLButtonElement).style.background = "transparent";
          }}
        >
          {isRestoring ? (
            <span
              className="inline-block w-3 h-3 rounded-full border-[1.5px] border-t-transparent animate-spin"
              style={{ borderColor: "#34d399", borderTopColor: "transparent" }}
            />
          ) : (
            <RotateCcw size={13} />
          )}
        </button>
        <button
          type="button"
          onClick={() => onPurge(item.trash_filename, item.title)}
          aria-label="Permanently delete"
          title="Endgültig löschen"
          className="rounded-md p-2 transition-colors"
          style={{
            color: "var(--color-text-muted)",
            background: "transparent",
            border: "1px solid rgba(255,255,255,0.04)",
            cursor: "pointer",
          }}
          onMouseEnter={(e) => {
            (e.currentTarget as HTMLButtonElement).style.color = STATUS_TEXT.error;
            (e.currentTarget as HTMLButtonElement).style.background =
              `${C.error}14`;
            (e.currentTarget as HTMLButtonElement).style.borderColor =
              `${C.error}38`;
          }}
          onMouseLeave={(e) => {
            (e.currentTarget as HTMLButtonElement).style.color =
              "var(--color-text-muted)";
            (e.currentTarget as HTMLButtonElement).style.background = "transparent";
            (e.currentTarget as HTMLButtonElement).style.borderColor =
              "rgba(255,255,255,0.04)";
          }}
        >
          <Trash2 size={13} />
        </button>
      </div>
    </div>
  );
}

// ── Page ──────────────────────────────────────────────────────────────────────

export function VaultTrashPage() {
  const queryClient = useQueryClient();
  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["vault", "trash"],
    queryFn: api.vault.trash.list,
    staleTime: 10_000,
  });

  // Track per-row restore errors (e.g. 409 when original path is occupied).
  const [restoreErrors, setRestoreErrors] = useState<Record<string, string>>({});

  const restore = useMutation({
    mutationFn: (filename: string) => api.vault.trash.restore(filename),
    onMutate: (filename) => {
      setRestoreErrors((prev) => {
        const next = { ...prev };
        delete next[filename];
        return next;
      });
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["vault"] });
    },
    onError: (err: Error, filename) => {
      setRestoreErrors((prev) => ({
        ...prev,
        [filename]: err.message || "Restore fehlgeschlagen",
      }));
    },
  });

  // Purge state — modal-driven.
  const [purgeTarget, setPurgeTarget] = useState<{ filename: string; title: string } | null>(null);
  const [purgeError, setPurgeError] = useState<string | null>(null);
  const purge = useMutation({
    mutationFn: (filename: string) => api.vault.trash.purge(filename),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["vault", "trash"] });
      setPurgeTarget(null);
      setPurgeError(null);
    },
    onError: (err: Error) => {
      setPurgeError(err.message || "Permanent-Delete fehlgeschlagen");
    },
  });

  const items = data?.items ?? [];

  return (
    <div className="flex flex-col">
      {isLoading && (
        <div className="flex flex-col">
          {Array.from({ length: 4 }).map((_, i) => (
            <div key={i} className="flex animate-pulse">
              <div className="shrink-0 w-[92px] py-5 pl-3 pr-4 flex flex-col items-end gap-1.5">
                <div className="h-7 w-9 rounded" style={{ background: "rgba(255,255,255,0.05)" }} />
                <div className="h-2.5 w-8 rounded" style={{ background: "rgba(255,255,255,0.04)" }} />
              </div>
              <div className="shrink-0 w-px self-stretch" style={{ background: "rgba(255,255,255,0.04)" }} />
              <div className="flex-1 py-5 px-5">
                <div className="h-4 w-1/2 rounded mb-2" style={{ background: "rgba(255,255,255,0.06)" }} />
                <div className="h-3 w-3/4 rounded" style={{ background: "rgba(255,255,255,0.04)" }} />
              </div>
            </div>
          ))}
        </div>
      )}

      {isError && (
        <div
          className="px-6 py-12 text-center"
          style={{ color: "var(--color-text-muted)", fontSize: "13.5px" }}
        >
          Papierkorb konnte nicht geladen werden: {(error as Error)?.message}
        </div>
      )}

      {!isLoading && !isError && items.length === 0 && (
        <div className="flex flex-col items-center justify-center py-24 px-6 text-center">
          <div
            className="w-14 h-14 rounded-full flex items-center justify-center mb-4"
            style={{
              background: "rgba(255,255,255,0.03)",
              border: "1px solid rgba(255,255,255,0.05)",
            }}
          >
            <ArchiveX size={20} style={{ color: "rgba(255,255,255,0.25)" }} />
          </div>
          <div
            className="font-mono uppercase font-semibold mb-1"
            style={{
              fontSize: "11px",
              letterSpacing: "0.16em",
              color: "var(--color-text-secondary)",
            }}
          >
            Papierkorb ist leer
          </div>
          <div
            style={{
              fontSize: "12.5px",
              color: "var(--color-text-muted)",
              maxWidth: "320px",
              lineHeight: 1.55,
            }}
          >
            Gelöschte Notes landen hier und können wiederhergestellt werden,
            bevor du sie endgültig entfernst.
          </div>
        </div>
      )}

      {!isLoading && !isError && items.length > 0 && (
        <>
          {/* Section header */}
          <div
            className="sticky top-0 z-10 flex items-baseline gap-3 px-4 py-3"
            style={{ background: "var(--color-bg-base)" }}
          >
            <span className="font-mono" style={{ fontSize: "10px", color: "rgba(255,255,255,0.35)" }}>
              ◆
            </span>
            <span
              className="font-mono uppercase font-semibold"
              style={{
                fontSize: "10px",
                letterSpacing: "0.18em",
                color: "var(--color-text-secondary)",
              }}
            >
              Papierkorb · {data?.count ?? 0}
            </span>
            <div
              className="flex-1 h-px"
              style={{
                background:
                  "linear-gradient(to right, rgba(255,255,255,0.08), rgba(255,255,255,0.02) 50%, transparent)",
              }}
            />
          </div>

          {items.map((item) => (
            <TrashRow
              key={item.trash_filename}
              item={item}
              onRestore={(fn) => restore.mutate(fn)}
              onPurge={(fn, title) => {
                setPurgeError(null);
                setPurgeTarget({ filename: fn, title });
              }}
              restoreError={restoreErrors[item.trash_filename] ?? null}
              restorePending={restore.isPending}
              restorePendingFor={restore.variables ?? null}
            />
          ))}
        </>
      )}

      <PurgeConfirmModal
        filename={purgeTarget?.filename ?? null}
        title={purgeTarget?.title ?? ""}
        onClose={() => {
          if (!purge.isPending) {
            setPurgeTarget(null);
            setPurgeError(null);
          }
        }}
        onConfirm={() => purgeTarget && purge.mutate(purgeTarget.filename)}
        isPurging={purge.isPending}
        error={purgeError}
      />
    </div>
  );
}
