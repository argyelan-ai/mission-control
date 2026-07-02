"use client";

/**
 * Phase 5 — inline preview of the binary attachment behind a deliverable
 * wrapper. Rendered below the wrapper's Markdown body in VaultReadingPanel.
 *
 * Why blob URLs: `<img>` and `<iframe>` cannot carry a Bearer header. We
 * fetch the protected endpoint with the JWT, get a blob, and feed it via
 * URL.createObjectURL. Same pattern as memory/AttachmentThumb.tsx.
 *
 * Rendering by MIME family:
 *   - application/pdf → `<iframe>` (browser-native PDF viewer)
 *   - image/*          → `<img>` thumbnail, click → fullsize modal
 *   - audio/*          → `<audio controls>` (future-proof for voice memos)
 *   - everything else  → "Download" link (the same blob URL, downloadable)
 *
 * The operator's Design-DNA: off-black panel, no generic borders, lazy load.
 */
import { useEffect, useState } from "react";
import { motion, AnimatePresence, useReducedMotion } from "framer-motion";
import { X } from "lucide-react";

import { api, getToken } from "@/lib/api";

interface Props {
  deliverableId: string;
  mime: string;
  sizeBytes?: number;
  title?: string;
}

export function AttachmentPreview({
  deliverableId,
  mime,
  sizeBytes,
  title,
}: Props) {
  const prefersReduce = useReducedMotion();
  const [objectUrl, setObjectUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [fullsizeOpen, setFullsizeOpen] = useState(false);

  useEffect(() => {
    let cancelled = false;
    let created: string | null = null;
    (async () => {
      try {
        const url = api.vault.getAttachmentUrl(deliverableId);
        const token = getToken();
        const resp = await fetch(url, {
          headers: token ? { Authorization: `Bearer ${token}` } : {},
        });
        if (!resp.ok) {
          if (!cancelled) setError(`HTTP ${resp.status}`);
          return;
        }
        const blob = await resp.blob();
        if (cancelled) return;
        created = URL.createObjectURL(blob);
        setObjectUrl(created);
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : "fetch error");
      }
    })();
    return () => {
      cancelled = true;
      if (created) URL.revokeObjectURL(created);
    };
  }, [deliverableId]);

  const family = mime.split("/")[0];
  const sizeLabel = sizeBytes ? `${Math.round(sizeBytes / 1024)} KB` : null;

  return (
    <div
      className="mt-6 rounded-lg overflow-hidden"
      style={{
        border: "1px solid rgba(255,255,255,0.06)",
        background: "rgba(255,255,255,0.02)",
      }}
    >
      <div
        className="px-4 py-2 flex items-center justify-between text-xs"
        style={{
          background: "rgba(0,0,0,0.25)",
          color: "rgba(255,255,255,0.7)",
          borderBottom: "1px solid rgba(255,255,255,0.04)",
        }}
      >
        <span className="font-mono tabular-nums">{mime}</span>
        {sizeLabel && (
          <span className="opacity-60 tabular-nums">{sizeLabel}</span>
        )}
      </div>

      {error && (
        <div
          className="p-4 text-sm"
          style={{ color: "rgba(239,68,68,0.9)" }}
        >
          Could not load attachment: {error}
        </div>
      )}

      {!error && !objectUrl && (
        <div
          className="p-6 text-sm opacity-60 animate-pulse"
          style={{ color: "rgba(255,255,255,0.6)" }}
        >
          Loading attachment…
        </div>
      )}

      {objectUrl && mime === "application/pdf" && (
        <iframe
          src={objectUrl}
          title={title || "PDF preview"}
          className="w-full"
          style={{ height: "640px", border: 0 }}
        />
      )}

      {objectUrl && family === "image" && (
        <button
          type="button"
          onClick={() => setFullsizeOpen(true)}
          className="block w-full"
          aria-label={`Bild vergrössern${title ? `: ${title}` : ""}`}
        >
          <motion.img
            src={objectUrl}
            alt={title || "Attachment"}
            loading="lazy"
            initial={prefersReduce ? false : { opacity: 0 }}
            animate={{ opacity: 1 }}
            className="w-full max-h-[480px] object-contain"
            style={{ background: "rgba(0,0,0,0.3)" }}
          />
        </button>
      )}

      {objectUrl && family === "audio" && (
        <div className="p-4">
          <audio controls src={objectUrl} className="w-full" />
        </div>
      )}

      {objectUrl &&
        mime !== "application/pdf" &&
        family !== "image" &&
        family !== "audio" && (
          <div className="p-4 text-sm" style={{ color: "rgba(255,255,255,0.75)" }}>
            <a
              href={objectUrl}
              download
              className="underline hover:opacity-100 opacity-80"
            >
              Download {title || "attachment"}
            </a>
          </div>
        )}

      <AnimatePresence>
        {fullsizeOpen && objectUrl && (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            className="fixed inset-0 z-50 flex items-center justify-center p-8"
            style={{ background: "rgba(0,0,0,0.85)" }}
            onClick={() => setFullsizeOpen(false)}
          >
            <button
              type="button"
              onClick={() => setFullsizeOpen(false)}
              className="absolute top-6 right-6 p-2 rounded"
              style={{ background: "rgba(255,255,255,0.1)" }}
              aria-label="Schliessen"
            >
              <X size={20} className="text-white" />
            </button>
            <motion.img
              src={objectUrl}
              alt={title || "Attachment fullsize"}
              initial={{ scale: 0.95 }}
              animate={{ scale: 1 }}
              exit={{ scale: 0.95 }}
              className="max-w-full max-h-full object-contain"
              onClick={(e) => e.stopPropagation()}
            />
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
