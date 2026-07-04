"use client";

/**
 * ProjectReferencesDialog — reference/asset files shared with every task in
 * a project (ADR-053). Opened from the project group header's paperclip
 * icon on the Tasks page (TaskListColumn).
 */

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Paperclip, X } from "lucide-react";
import { api } from "@/lib/api";
import { notify } from "@/lib/notify";
import { C } from "@/lib/colors";
import { REFERENCE_FILE_ACCEPT } from "@/lib/utils";
import { ResponsiveModal } from "@/components/shared/ResponsiveModal";
import { ReferenceFileRow } from "@/components/shared/ReferenceFileRow";

export function ProjectReferencesDialog({
  open,
  onClose,
  projectId,
  projectName,
}: {
  open: boolean;
  onClose: () => void;
  projectId: string;
  projectName: string;
}) {
  const qc = useQueryClient();
  const queryKey = ["references", "project", projectId];

  const { data: references = [] } = useQuery({
    queryKey,
    queryFn: () => api.references.list({ projectId }),
    enabled: open,
  });

  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);

  const deleteMutation = useMutation({
    mutationFn: (id: string) => api.references.remove(id),
    onSuccess: () => qc.invalidateQueries({ queryKey }),
    onError: () => notify.error("Delete failed"),
  });

  async function onPick(e: React.ChangeEvent<HTMLInputElement>) {
    const files = Array.from(e.target.files ?? []);
    e.target.value = "";
    if (files.length === 0) return;
    setUploadError(null);
    setUploading(true);
    try {
      for (const file of files) {
        await api.references.upload({ projectId }, file);
      }
      await qc.invalidateQueries({ queryKey });
    } catch (err) {
      setUploadError(err instanceof Error ? err.message : "Upload failed");
    } finally {
      setUploading(false);
    }
  }

  return (
    <ResponsiveModal open={open} onClose={onClose} aria-label={`References for ${projectName}`}>
      <div className="flex items-center justify-between gap-3 px-5 py-3.5 shrink-0" style={{ borderBottom: `1px solid ${C.border}` }}>
        <div className="min-w-0">
          <span className="block text-sm font-semibold truncate" style={{ color: C.textPrimary }}>{projectName}</span>
          <span className="block text-[10px] mt-0.5" style={{ color: C.textMuted }}>
            Agents receive these files with every task in this project.
          </span>
        </div>
        <button
          onClick={onClose}
          aria-label="Close"
          className="shrink-0 cursor-pointer hover:opacity-80 transition-opacity"
          style={{ color: C.textMuted }}
        >
          <X size={16} />
        </button>
      </div>

      <div className="p-5 overflow-y-auto flex-1 flex flex-col gap-1.5">
        {references.length === 0 && (
          <div className="text-xs text-center py-6" style={{ color: C.textMuted }}>
            No reference files yet.
          </div>
        )}
        {references.map((r) => (
          <ReferenceFileRow
            key={r.id}
            reference={r}
            onDelete={() => deleteMutation.mutate(r.id)}
            deleting={deleteMutation.isPending && deleteMutation.variables === r.id}
          />
        ))}
        <label
          className="inline-flex items-center gap-1.5 self-start px-2.5 py-1.5 rounded-lg text-[11px] cursor-pointer transition-colors mt-1"
          style={{ border: `1px dashed ${C.border}`, color: C.textMuted }}
        >
          <Paperclip size={11} />
          {uploading ? "Uploading…" : "Add files"}
          <input
            type="file"
            multiple
            accept={REFERENCE_FILE_ACCEPT}
            onChange={onPick}
            className="hidden"
            disabled={uploading}
          />
        </label>
        {uploadError && <span className="text-[10px]" style={{ color: C.error }}>{uploadError}</span>}
      </div>
    </ResponsiveModal>
  );
}
