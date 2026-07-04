"use client";

/**
 * ReferenceFileRow — one reference/asset file (ADR-053). Shared between
 * TaskReferences (task detail) and ProjectReferencesDialog (project group
 * header) since both list the same shape and offer the same actions.
 */

import { useState } from "react";
import { Paperclip, Download, Trash2, X } from "lucide-react";
import { api } from "@/lib/api";
import { notify } from "@/lib/notify";
import { C } from "@/lib/colors";
import { formatBytes } from "@/lib/utils";
import type { ReferenceFile } from "@/lib/types";

export function ReferenceFileRow({
  reference,
  onDelete,
  deleting,
}: {
  reference: ReferenceFile;
  onDelete: () => void;
  deleting: boolean;
}) {
  const [confirm, setConfirm] = useState(false);
  const [downloading, setDownloading] = useState(false);

  const handleDownload = async () => {
    setDownloading(true);
    try {
      const url = await api.references.fetchBlob(reference.id);
      const a = document.createElement("a");
      a.href = url;
      a.download = reference.original_name;
      a.click();
      URL.revokeObjectURL(url);
    } catch {
      notify.error("Download failed");
    } finally {
      setDownloading(false);
    }
  };

  return (
    <div
      className="flex items-center gap-2 px-2.5 py-2 rounded-lg text-xs"
      style={{ background: C.bgSurface, border: `1px solid ${C.border}` }}
    >
      <Paperclip size={11} style={{ color: C.textMuted, flexShrink: 0 }} />
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-1.5">
          <span className="truncate" style={{ color: C.textPrimary }}>{reference.original_name}</span>
          {reference.inherited && (
            <span
              className="shrink-0 text-[9px] px-1.5 py-px rounded uppercase tracking-[0.04em]"
              style={{ color: C.textMuted, border: `1px solid ${C.border}` }}
            >
              from project
            </span>
          )}
        </div>
        {reference.note && (
          <div className="truncate text-[10px] mt-0.5" style={{ color: C.textMuted }}>{reference.note}</div>
        )}
      </div>
      <span className="shrink-0 text-[10px]" style={{ color: C.textDim }}>{formatBytes(reference.size)}</span>
      <button
        type="button"
        onClick={handleDownload}
        disabled={downloading}
        aria-label={`Download ${reference.original_name}`}
        className="shrink-0 cursor-pointer hover:opacity-80"
        style={{ color: C.textMuted }}
      >
        <Download size={12} />
      </button>
      {!reference.inherited && (
        confirm ? (
          <div className="flex items-center gap-1 shrink-0">
            <button
              type="button"
              onClick={onDelete}
              disabled={deleting}
              className="text-[10px] font-semibold px-1.5 py-0.5 rounded cursor-pointer"
              style={{ backgroundColor: `${C.error}26`, color: "#D05F5F" }}
            >
              {deleting ? "…" : "Delete"}
            </button>
            <button
              type="button"
              onClick={() => setConfirm(false)}
              className="cursor-pointer"
              style={{ color: C.textMuted }}
              aria-label="Cancel delete"
            >
              <X size={11} />
            </button>
          </div>
        ) : (
          <button
            type="button"
            onClick={() => setConfirm(true)}
            aria-label={`Delete ${reference.original_name}`}
            className="shrink-0 cursor-pointer hover:opacity-80"
            style={{ color: C.textMuted }}
          >
            <Trash2 size={12} />
          </button>
        )
      )}
    </div>
  );
}
