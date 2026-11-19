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
 * Every word comes from i18n and the type reads as words ("HTML file"), not
 * the MIME string (round-4 user test t4 NICE-2: "text/html · Download …" in
 * a German UI).
 *
 * The operator's Design-DNA: off-black panel, no generic borders, lazy load.
 */
import { useEffect, useState } from "react";
import { motion, AnimatePresence, useReducedMotion } from "framer-motion";
import { X } from "lucide-react";
import { useTranslations } from "next-intl";

import { api, getToken } from "@/lib/api";
import { useBodyScrollLock } from "@/hooks/useBodyScrollLock";

import { C, alpha } from "@/lib/colors";

type Translate = (key: string, values?: Record<string, string>) => string;

const SPREADSHEET = /spreadsheet|ms-excel/;
const DOCUMENT = /wordprocessing|msword|opendocument\.text/;

/** A MIME type in words for the card ("text/html" → "HTML file"). */
export function fileKindLabel(mime: string, t: Translate): string {
  const [family = "", sub = ""] = mime.toLowerCase().split(";")[0].trim().split("/");
  if (!family || !sub) return t("fileKind.unknown");
  // "svg+xml" → "SVG", "vnd.foo.bar" → "BAR"
  const ext = (sub.split("+")[0].split(".").pop() ?? sub).toUpperCase();
  if (sub === "html") return t("fileKind.html");
  if (sub === "pdf") return t("fileKind.pdf");
  if (family === "image") return t("fileKind.image", { ext });
  if (family === "audio") return t("fileKind.audio", { ext });
  if (family === "video") return t("fileKind.video", { ext });
  if (sub === "markdown" || sub === "x-markdown") return t("fileKind.markdown");
  if (sub === "csv") return t("fileKind.csv");
  if (sub === "json") return t("fileKind.json");
  if (SPREADSHEET.test(sub)) return t("fileKind.spreadsheet");
  if (DOCUMENT.test(sub)) return t("fileKind.document");
  if (family === "text") return t("fileKind.text");
  return t("fileKind.other", { ext });
}

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
  const t = useTranslations("vault");
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

  // Fullsize modal chrome (rule 4): iOS-safe scroll lock + Esc close.
  // Esc is captured at the window so it does not also bubble into the
  // VaultReadingPanel mobile sheet's own Esc handler (which would close the
  // whole note underneath the lightbox).
  useBodyScrollLock(fullsizeOpen);
  useEffect(() => {
    if (!fullsizeOpen) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        setFullsizeOpen(false);
      }
    };
    window.addEventListener("keydown", handler, true);
    return () => window.removeEventListener("keydown", handler, true);
  }, [fullsizeOpen]);

  return (
    <div
      className="mt-6 rounded-lg overflow-hidden"
      style={{
        border: "1px solid var(--color-border)",
        background: "var(--color-bg-elevated)",
      }}
    >
      <div
        className="px-4 py-2 flex items-center justify-between text-xs light:bg-[var(--color-bg-elevated)]!"
        style={{
          background: alpha(C.scrim, 0.25),
          color: "var(--color-text-muted)",
          borderBottom: "1px solid var(--color-border-subtle)",
        }}
      >
        <span title={mime}>{fileKindLabel(mime, t)}</span>
        {sizeLabel && (
          <span className="tabular-nums" style={{ color: "var(--color-text-muted)" }}>{sizeLabel}</span>
        )}
      </div>

      {error && (
        <div
          className="p-4 text-sm"
          style={{ color: alpha(C.error, 0.9) }}
        >
          {t("attachmentLoadFailed", { error })}
        </div>
      )}

      {!error && !objectUrl && (
        <div
          className="p-6 text-sm opacity-60 animate-pulse"
          style={{ color: "var(--color-text-secondary)" }}
        >
          {t("attachmentLoading")}
        </div>
      )}

      {objectUrl && mime === "application/pdf" && (
        <iframe
          src={objectUrl}
          title={title || t("attachmentPdfPreview")}
          className="w-full"
          style={{ height: "640px", border: 0 }}
        />
      )}

      {objectUrl && family === "image" && (
        <button
          type="button"
          onClick={() => setFullsizeOpen(true)}
          className="block w-full"
          aria-label={title ? t("attachmentEnlargeNamed", { name: title }) : t("attachmentEnlarge")}
        >
          <motion.img
            src={objectUrl}
            alt={title || t("attachment")}
            loading="lazy"
            initial={prefersReduce ? false : { opacity: 0 }}
            animate={{ opacity: 1 }}
            className="w-full max-h-[480px] object-contain"
            style={{ background: alpha(C.scrim, 0.3) }}
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
          <div className="p-4 text-sm" style={{ color: "var(--color-text-secondary)" }}>
            <a
              href={objectUrl}
              download
              className="underline"
            >
              {t("attachmentDownload", { name: title || t("attachmentFallbackName") })}
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
            style={{ background: alpha(C.scrim, 0.85) }}
            onClick={() => setFullsizeOpen(false)}
          >
            <button
              type="button"
              onClick={() => setFullsizeOpen(false)}
              className="absolute top-6 right-6 p-3 rounded-sm"
              style={{ background: "var(--color-bg-hover)" }}
              aria-label={t("attachmentClose")}
            >
              <X size={20} style={{ color: "var(--color-text-primary)" }} />
            </button>
            <motion.img
              src={objectUrl}
              alt={title || t("attachment")}
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
