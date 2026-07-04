"use client";

/**
 * TaskReferences — the task detail's "References" section (ADR-053). Shows
 * the task's own uploads plus files inherited from its project (read-only,
 * badged "from project" — deleting those belongs to the project dialog).
 * Agents read these files directly; their paths flow into the dispatch
 * directive automatically, no action needed here beyond uploading them.
 */

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Paperclip } from "lucide-react";
import { api } from "@/lib/api";
import { notify } from "@/lib/notify";
import { C } from "@/lib/colors";
import { REFERENCE_FILE_ACCEPT } from "@/lib/utils";
import { ReferenceFileRow } from "@/components/shared/ReferenceFileRow";

export function TaskReferences({ taskId }: { taskId: string }) {
  const qc = useQueryClient();
  const queryKey = ["references", "task", taskId];

  const { data: references = [] } = useQuery({
    queryKey,
    queryFn: () => api.references.list({ taskId }),
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
        await api.references.upload({ taskId }, file);
      }
      await qc.invalidateQueries({ queryKey });
    } catch (err) {
      setUploadError(err instanceof Error ? err.message : "Upload failed");
    } finally {
      setUploading(false);
    }
  }

  const uploadButton = (
    <label
      className="inline-flex items-center gap-1.5 self-start px-2.5 py-1.5 rounded-lg text-[11px] cursor-pointer transition-colors"
      style={{ border: `1px dashed ${C.border}`, color: C.textMuted }}
    >
      <Paperclip size={11} />
      {uploading ? "Uploading…" : references.length > 0 ? "Add more" : "Add reference files"}
      <input
        type="file"
        multiple
        accept={REFERENCE_FILE_ACCEPT}
        onChange={onPick}
        className="hidden"
        disabled={uploading}
      />
    </label>
  );

  return (
    <div className="flex flex-col gap-1.5">
      {references.map((r) => (
        <ReferenceFileRow
          key={r.id}
          reference={r}
          onDelete={() => deleteMutation.mutate(r.id)}
          deleting={deleteMutation.isPending && deleteMutation.variables === r.id}
        />
      ))}
      {uploadButton}
      {uploadError && <span className="text-[10px]" style={{ color: C.error }}>{uploadError}</span>}
    </div>
  );
}
