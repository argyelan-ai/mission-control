"use client";

/**
 * Phase 5 MSY-03: Container that lists attachments below the memory body.
 *
 * v0.5 contract (W7): rendered with `editMode={false}` from `/memory/page.tsx`
 * — the in-component upload + delete affordances exist but are gated behind
 * `editMode`. A follow-up plan will wire the modal's edit-mode toggle through
 * to flip this flag. Backend POST/DELETE endpoints remain reachable.
 *
 * - Renders nothing if attachments empty + editMode false.
 * - Copy per UI-SPEC.
 * - The operator's Design-DNA: glass surface, off-black, no Inter/Roboto/Arial.
 */
import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Upload } from "lucide-react";

import { api } from "@/lib/api";
import type { BoardMemory, BoardMemoryAttachment } from "@/lib/types";
import { STATUS_TEXT } from "@/lib/colors";

import { AttachmentThumb } from "./AttachmentThumb";
import { AttachmentLightbox } from "./AttachmentLightbox";

interface Props {
  entry: BoardMemory;
  editMode?: boolean;
}

const ALLOWED_MIMES = "image/png,image/jpeg,image/gif,image/webp,application/pdf";

export function AttachmentPanel({ entry, editMode }: Props) {
  const queryClient = useQueryClient();
  const attachments = entry.attachments ?? [];
  const [lightbox, setLightbox] = useState<{
    src: string;
    attachment: BoardMemoryAttachment;
  } | null>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);

  if (!editMode && attachments.length === 0) return null;

  async function invalidate() {
    await queryClient.invalidateQueries({ queryKey: ["knowledge"] });
    await queryClient.invalidateQueries({ queryKey: ["knowledge-layer-episodic"] });
    await queryClient.invalidateQueries({ queryKey: ["knowledge-layer-semantic"] });
    await queryClient.invalidateQueries({ queryKey: ["knowledge-layer-agent"] });
  }

  async function onPick(e: React.ChangeEvent<HTMLInputElement>) {
    const files = Array.from(e.target.files ?? []);
    setUploadError(null);
    for (const f of files) {
      if (attachments.length >= 5) {
        setUploadError("Maximum of 5 attachments per entry reached");
        break;
      }
      setUploading(true);
      try {
        await api.knowledge.uploadAttachment(entry.id, f);
      } catch (err: unknown) {
        const msg = (err as { message?: string }).message ?? "";
        if (msg.includes("415")) {
          setUploadError("File type not allowed. Allowed: PNG, JPEG, GIF, WebP, PDF");
        } else if (msg.includes("413")) {
          setUploadError("File too large. Maximum size: 10 MB");
        } else if (msg.includes("400")) {
          setUploadError("Maximum of 5 attachments per entry reached");
        } else {
          setUploadError("Upload failed — please try again");
        }
        break;
      } finally {
        setUploading(false);
      }
    }
    await invalidate();
    e.target.value = "";
  }

  async function onDelete(filename: string) {
    try {
      await api.knowledge.deleteAttachment(entry.id, filename);
      await invalidate();
    } catch {
      setUploadError("Attachment could not be deleted");
    }
  }

  return (
    <section
      className="rounded-xl p-4 mt-5"
      style={{
        background: "rgba(255,255,255,0.02)",
        border: "1px solid rgba(255,255,255,0.05)",
      }}
    >
      <h3
        className="text-xs font-semibold uppercase tracking-wider mb-3"
        style={{ color: "rgba(255,255,255,0.7)" }}
      >
        {attachments.length === 1 ? "1 attachment" : `${attachments.length} attachments`}
      </h3>
      {attachments.length === 0 && editMode && (
        <p className="text-xs mb-3" style={{ color: "rgba(255,255,255,0.6)" }}>
          No attachments yet — images and PDFs can be uploaded (max. 10 MB per file,
          max. 5 files)
        </p>
      )}
      <div className="flex flex-wrap gap-2">
        {attachments.map((a, i) => (
          <AttachmentThumb
            key={a.path}
            attachment={a}
            entryId={entry.id}
            index={i}
            editMode={editMode}
            onClickImage={(src, attachment) => setLightbox({ src, attachment })}
            onDelete={onDelete}
          />
        ))}
        {editMode && attachments.length < 5 && (
          <label
            className="inline-flex items-center gap-2 px-3 py-2 rounded-lg cursor-pointer text-xs"
            style={{
              border: "1px dashed rgba(255,255,255,0.15)",
              color: "rgba(255,255,255,0.8)",
            }}
          >
            <Upload size={14} aria-hidden />
            <span>{uploading ? "Uploading…" : "Upload file"}</span>
            <input
              type="file"
              multiple
              accept={ALLOWED_MIMES}
              onChange={onPick}
              className="hidden"
              disabled={uploading}
            />
          </label>
        )}
      </div>
      {uploadError && (
        <p className="text-xs mt-3" style={{ color: STATUS_TEXT.error }} role="alert">
          {uploadError}
        </p>
      )}
      <AttachmentLightbox
        open={lightbox != null}
        src={lightbox?.src ?? null}
        filename={lightbox?.attachment.original_name ?? ""}
        sizeKb={lightbox ? Math.round(lightbox.attachment.size_bytes / 1024) : 0}
        onClose={() => {
          if (lightbox) URL.revokeObjectURL(lightbox.src);
          setLightbox(null);
        }}
      />
    </section>
  );
}
