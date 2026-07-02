"use client";

/**
 * Phase 5 MSY-03: Single attachment thumbnail card.
 *
 * - Image MIME → 120×80 image preview (auth-fetched as blob → object URL).
 *   Click opens the lightbox.
 * - Non-image MIME (PDF, etc.) → 80×80 icon-card with original_name + "Öffnen" link.
 * - Lazy-loaded (`loading="lazy"`).
 * - Framer Motion stagger entry; respects `useReducedMotion`.
 * - The operator's Design-DNA: glass surface (rgba), no Inter/Roboto/Arial, off-black bg.
 */
import { useEffect, useState } from "react";
import { motion, useReducedMotion } from "framer-motion";
import { FileText, Image as ImageIcon, X } from "lucide-react";

import { api, getToken } from "@/lib/api";
import type { BoardMemoryAttachment } from "@/lib/types";

interface Props {
  attachment: BoardMemoryAttachment;
  entryId: string;
  index: number;
  onClickImage?: (objectUrl: string, attachment: BoardMemoryAttachment) => void;
  onDelete?: (filename: string) => void;
  editMode?: boolean;
}

export function AttachmentThumb({
  attachment,
  entryId,
  index,
  onClickImage,
  onDelete,
  editMode,
}: Props) {
  const prefersReduce = useReducedMotion();
  const [objectUrl, setObjectUrl] = useState<string | null>(null);
  const filename = attachment.path.split("/").pop() ?? "file";
  const isImage = attachment.mime_type.startsWith("image/");

  useEffect(() => {
    if (!isImage) return;
    let cancelled = false;
    let createdUrl: string | null = null;
    (async () => {
      try {
        const url = api.knowledge.getAttachmentUrl(entryId, filename);
        const token = getToken();
        const resp = await fetch(url, {
          headers: token ? { Authorization: `Bearer ${token}` } : {},
        });
        if (!resp.ok || cancelled) return;
        const blob = await resp.blob();
        if (cancelled) return;
        createdUrl = URL.createObjectURL(blob);
        setObjectUrl(createdUrl);
      } catch {
        // Network/auth error — leave placeholder icon.
      }
    })();
    return () => {
      cancelled = true;
      if (createdUrl) URL.revokeObjectURL(createdUrl);
    };
  }, [entryId, filename, isImage]);

  return (
    <motion.div
      initial={prefersReduce ? false : { opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: index * 0.04, duration: 0.2 }}
      className="group relative rounded-lg overflow-hidden"
      style={{
        border: "1px solid rgba(255,255,255,0.06)",
        background: "rgba(255,255,255,0.02)",
      }}
    >
      {isImage ? (
        <button
          type="button"
          onClick={() => objectUrl && onClickImage?.(objectUrl, attachment)}
          className="block w-[120px] h-[80px]"
          aria-label={`Bild "${attachment.original_name}" vergrössern`}
        >
          {objectUrl ? (
            <img
              src={objectUrl}
              alt={attachment.original_name}
              loading="lazy"
              className="w-full h-full object-cover"
            />
          ) : (
            <div className="w-full h-full flex items-center justify-center opacity-50">
              <ImageIcon size={20} aria-hidden />
            </div>
          )}
        </button>
      ) : (
        <a
          href={api.knowledge.getAttachmentUrl(entryId, filename)}
          target="_blank"
          rel="noreferrer noopener"
          className="flex flex-col items-center justify-center w-[80px] h-[80px] text-center p-1 text-[10px]"
          style={{ color: "rgba(255,255,255,0.7)" }}
          title={`Öffnen: ${attachment.original_name}`}
        >
          <FileText size={20} aria-hidden />
          <span className="mt-1 truncate max-w-full" title={attachment.original_name}>
            {attachment.original_name}
          </span>
        </a>
      )}
      <div
        className="absolute bottom-0 left-0 right-0 px-2 py-1 text-[10px] tabular-nums opacity-70 truncate"
        style={{ background: "rgba(10,10,15,0.6)", color: "rgba(255,255,255,0.85)" }}
      >
        {Math.round(attachment.size_bytes / 1024)} KB
      </div>
      {editMode && onDelete && (
        <button
          type="button"
          onClick={() => {
            if (window.confirm(`Anhang "${attachment.original_name}" löschen?`)) {
              onDelete(filename);
            }
          }}
          className="absolute top-1 right-1 p-1 rounded opacity-0 group-hover:opacity-100 transition-opacity touch-visible"
          style={{ background: "rgba(239,68,68,0.8)" }}
          aria-label={`Anhang ${attachment.original_name} löschen`}
        >
          <X size={12} className="text-white" />
        </button>
      )}
    </motion.div>
  );
}
