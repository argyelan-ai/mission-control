"use client";

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Archive, ArchiveRestore, Trash2, Loader2 } from "lucide-react";
import { api } from "@/lib/api";
import { notify } from "@/lib/notify";
import { C } from "@/lib/colors";
import type { Agent } from "@/lib/types";

// ── Agent lifecycle actions (Phase: archive → delete) ────────────────────────
// Single-responsibility control cluster, shared by the agent detail page and
// the "Archiviert" section of the agents list. Conditional on `archived_at`:
//   • active   → Archivieren + Löschen (disabled, "Erst archivieren")
//   • archived → Wiederherstellen + Löschen (enabled, inline confirm)
// Backend 409 (busy) / 422 (singleton bridge) `detail` is surfaced verbatim in
// the toast — never swallowed (see extractDetail).

/**
 * Pull the human-readable message out of a request() error. api.ts throws
 * `Error("API 409: {\"detail\":\"…\"}")`; FastAPI's detail is either a plain
 * string or a `{ message }` object. Fall back to the raw message.
 */
export function extractDetail(err: unknown): string {
  const msg = err instanceof Error ? err.message : String(err);
  const brace = msg.indexOf("{");
  if (brace !== -1) {
    try {
      const body = JSON.parse(msg.slice(brace));
      const detail = body?.detail;
      if (typeof detail === "string") return detail;
      if (detail && typeof detail === "object" && typeof detail.message === "string") {
        return detail.message;
      }
    } catch {
      /* not JSON — fall through to raw message */
    }
  }
  return msg;
}

// Mirrors the detail page's ActionButton visual language (color-tinted flat
// pill, no glow — Leitstand doctrine) without importing it (it's page-local).
function LifecycleButton({
  icon: Icon,
  label,
  color,
  onClick,
  loading,
  disabled,
  title,
}: {
  icon: typeof Archive;
  label: string;
  color: string;
  onClick: () => void;
  loading?: boolean;
  disabled?: boolean;
  title?: string;
}) {
  return (
    <button
      onClick={onClick}
      disabled={loading || disabled}
      title={title}
      className="flex items-center justify-center gap-1.5 text-[11px] px-3 py-1.5 max-sm:py-3 max-sm:min-h-touch rounded-lg cursor-pointer transition-all disabled:opacity-40 disabled:cursor-not-allowed"
      style={{
        backgroundColor: `${color}18`,
        color,
        border: `1px solid ${color}30`,
      }}
    >
      {loading ? <Loader2 size={12} className="animate-spin" /> : <Icon size={12} />}
      {label}
    </button>
  );
}

export function AgentActions({
  agent,
  onDeleted,
}: {
  agent: Agent;
  /** Called after a successful hard delete (e.g. navigate away). */
  onDeleted?: () => void;
}) {
  const qc = useQueryClient();
  const [confirmDelete, setConfirmDelete] = useState(false);
  const isArchived = agent.archived_at != null;

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["agents"] });
    qc.invalidateQueries({ queryKey: ["agent", agent.id] });
  };

  const archiveMutation = useMutation({
    mutationFn: () => api.agents.archive(agent.id),
    onSuccess: () => {
      notify.success(`${agent.name} archiviert`);
      invalidate();
    },
    onError: (e) => notify.error(extractDetail(e)),
  });

  const restoreMutation = useMutation({
    mutationFn: () => api.agents.restore(agent.id),
    onSuccess: () => {
      notify.success(`${agent.name} wiederhergestellt`);
      invalidate();
    },
    onError: (e) => notify.error(extractDetail(e)),
  });

  const deleteMutation = useMutation({
    mutationFn: () => api.agents.delete(agent.id),
    onSuccess: () => {
      notify.success(`${agent.name} gelöscht`);
      setConfirmDelete(false);
      invalidate();
      onDeleted?.();
    },
    onError: (e) => {
      notify.error(extractDetail(e));
      setConfirmDelete(false);
    },
  });

  return (
    <div className="flex items-center gap-2 flex-wrap">
      {isArchived ? (
        <LifecycleButton
          icon={ArchiveRestore}
          label="Wiederherstellen"
          color={C.online}
          onClick={() => restoreMutation.mutate()}
          loading={restoreMutation.isPending}
          title="Agent wiederherstellen — Runtime wird neu gestartet"
        />
      ) : (
        <LifecycleButton
          icon={Archive}
          label="Archivieren"
          color={C.warning}
          onClick={() => archiveMutation.mutate()}
          loading={archiveMutation.isPending}
          title="Agent archivieren — stoppt die Runtime, behält DB + Dateien"
        />
      )}

      {confirmDelete && isArchived ? (
        <span className="flex items-center gap-2">
          <span className="text-[11px]" style={{ color: C.error }}>
            Sicher?
          </span>
          <button
            onClick={() => deleteMutation.mutate()}
            disabled={deleteMutation.isPending}
            className="flex items-center gap-1 text-[11px] px-2.5 py-1.5 max-sm:py-3 max-sm:min-h-touch rounded-lg cursor-pointer font-medium disabled:opacity-50"
            style={{ backgroundColor: `${C.error}26`, color: C.error }}
          >
            {deleteMutation.isPending && <Loader2 size={11} className="animate-spin" />}
            Ja, löschen
          </button>
          <button
            onClick={() => setConfirmDelete(false)}
            className="text-[11px] px-2.5 py-1.5 max-sm:py-3 max-sm:min-h-touch rounded-lg cursor-pointer text-[var(--color-text-muted)]"
          >
            Abbrechen
          </button>
        </span>
      ) : (
        <LifecycleButton
          icon={Trash2}
          label="Löschen"
          color={C.error}
          onClick={() => isArchived && setConfirmDelete(true)}
          disabled={!isArchived}
          title={isArchived ? "Agent endgültig löschen" : "Erst archivieren"}
        />
      )}
    </div>
  );
}
