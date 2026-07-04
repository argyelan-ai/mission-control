"use client";

/**
 * DeliverablesTab — screenshots / files an agent attached to a task.
 * Extracted from TaskDetailPanel (07/2026 redesign); AuthImage + Lightbox
 * live here because only deliverables render authenticated previews.
 */

import { useState, useEffect } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { Image as ImageIcon, X, ZoomIn } from "lucide-react";
import { getToken } from "@/lib/api";
import { C } from "@/lib/colors";
import { DeliverableCard } from "./DeliverableCard";
import type { TaskDeliverable } from "@/lib/types";

// ── Authenticated Image ──────────────────────────────────────────────────────

function AuthImage({
  src, alt, className, style, onError,
}: {
  src: string;
  alt: string;
  className?: string;
  style?: React.CSSProperties;
  onError?: () => void;
}) {
  const [blobUrl, setBlobUrl] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let active = true;
    let objectUrl: string | null = null;

    fetch(src, { headers: { Authorization: `Bearer ${getToken()}` } })
      .then((r) => {
        if (!r.ok) throw new Error(`${r.status}`);
        return r.blob();
      })
      .then((blob) => {
        if (!active) return;
        objectUrl = URL.createObjectURL(blob);
        setBlobUrl(objectUrl);
      })
      .catch(() => {
        if (!active) return;
        setFailed(true);
        onError?.();
      });

    return () => {
      active = false;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [src]);

  if (failed) return null;
  if (!blobUrl) {
    return (
      <div
        className={className}
        style={{ ...style, background: "rgba(255,255,255,0.03)", display: "flex", alignItems: "center", justifyContent: "center" }}
      >
        <div className="w-4 h-4 rounded-full border-2 border-t-transparent animate-spin" style={{ borderColor: `${C.accent}40`, borderTopColor: "transparent" }} />
      </div>
    );
  }

  return <img src={blobUrl} alt={alt} className={className} style={style} />;
}

// ── Image Lightbox ───────────────────────────────────────────────────────────

function ImageLightbox({
  src, alt, onClose,
}: { src: string; alt: string; onClose: () => void }) {
  return (
    <AnimatePresence>
      <motion.div
        key="lightbox"
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        transition={{ duration: 0.15 }}
        className="fixed inset-0 z-[200] flex items-center justify-center p-4"
        style={{ background: "rgba(0,0,0,0.88)", backdropFilter: "blur(12px)" }}
        onClick={onClose}
      >
        <motion.div
          initial={{ scale: 0.92, opacity: 0 }}
          animate={{ scale: 1, opacity: 1 }}
          exit={{ scale: 0.92, opacity: 0 }}
          transition={{ duration: 0.2, ease: [0.16, 1, 0.3, 1] }}
          className="relative max-w-[90vw] max-h-[88vh]"
          onClick={(e) => e.stopPropagation()}
        >
          {/* Full-page screenshots are far taller than the viewport — show
              them at full width and let the operator scroll through the page
              instead of squeezing everything into 85vh. */}
          <div
            className="overflow-y-auto overflow-x-hidden rounded-xl"
            style={{ maxWidth: "90vw", maxHeight: "82vh", boxShadow: "0 0 60px rgba(0,0,0,0.8)" }}
          >
            <AuthImage src={src} alt={alt} className="block w-full h-auto" />
          </div>
          <button
            onClick={onClose}
            className="absolute top-2 right-2 flex items-center justify-center w-8 h-8 rounded-full cursor-pointer"
            style={{ background: "rgba(0,0,0,0.6)", color: C.textPrimary, border: `1px solid ${C.borderActive}` }}
            aria-label="Close"
          >
            <X size={14} />
          </button>
          <div className="text-center mt-2 text-xs" style={{ color: C.textMuted }}>{alt}</div>
        </motion.div>
      </motion.div>
    </AnimatePresence>
  );
}

// ── Deliverables Tab ─────────────────────────────────────────────────────────

export function DeliverablesTab({
  deliverables,
  boardId,
  taskId,
}: {
  deliverables: TaskDeliverable[];
  boardId: string;
  taskId: string;
}) {
  const [lightbox, setLightbox] = useState<{ src: string; alt: string } | null>(null);

  if (!deliverables.length) {
    return (
      <div className="flex flex-col items-center justify-center py-10 gap-2">
        <ImageIcon size={24} style={{ color: C.bgHover }} />
        <p className="text-sm" style={{ color: C.textMuted }}>No deliverables</p>
      </div>
    );
  }

  const screenshots = deliverables.filter((d) => d.deliverable_type === "screenshot" && d.path);
  const others = deliverables.filter((d) => d.deliverable_type !== "screenshot" || !d.path);

  // Subtask count: when include_subtasks=true, how many come from children?
  const fromSubtasks = deliverables.filter((d) => (d.source_depth ?? 0) > 0).length;

  return (
    <div className="space-y-4">
      {fromSubtasks > 0 && (
        <div
          className="text-[10px] px-2.5 py-1.5 rounded flex items-center gap-2"
          style={{
            background: C.accentSubtle,
            border: `1px solid ${C.borderAccent}`,
            color: C.accent,
          }}
        >
          <span>{deliverables.length - fromSubtasks} from this task</span>
          <span style={{ color: C.textMuted }}>·</span>
          <span>{fromSubtasks} from subtasks (marked with ← badge)</span>
        </div>
      )}

      {/* Screenshot grid */}
      {screenshots.length > 0 && (
        <div>
          <div
            className="text-[10px] font-semibold uppercase tracking-[0.07em] mb-2"
            style={{ color: C.textDim }}
          >
            Screenshots ({screenshots.length})
          </div>
          <div
            className="grid gap-2"
            style={{ gridTemplateColumns: "repeat(auto-fill, minmax(140px, 1fr))" }}
          >
            {screenshots.map((d, i) => (
              <motion.button
                key={d.id}
                initial={{ opacity: 0, y: 4 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ delay: i * 0.04, duration: 0.18, ease: "easeOut" }}
                className="group relative rounded-xl overflow-hidden cursor-pointer"
                style={{
                  aspectRatio: "16/10",
                  background: "rgba(255,255,255,0.02)",
                  border: `1px solid ${C.border}`,
                }}
                onClick={() =>
                  setLightbox({
                    src: `/api/v1/boards/${boardId}/tasks/${taskId}/deliverables/${d.id}/image`,
                    alt: d.title,
                  })
                }
                onKeyDown={(e) => e.key === "Enter" && setLightbox({
                  src: `/api/v1/boards/${boardId}/tasks/${taskId}/deliverables/${d.id}/image`,
                  alt: d.title,
                })}
                aria-label={`View screenshot: ${d.title}`}
              >
                <AuthImage
                  src={`/api/v1/boards/${boardId}/tasks/${taskId}/deliverables/${d.id}/image`}
                  alt={d.title}
                  className="w-full h-full object-cover transition-transform duration-200 group-hover:scale-[1.03]"
                  style={{ objectPosition: "top" }}
                />
                {/* Hover overlay */}
                <div
                  className="absolute inset-0 flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity duration-150 touch-visible"
                  style={{ background: "rgba(0,0,0,0.55)" }}
                >
                  <ZoomIn size={18} style={{ color: "white" }} />
                </div>
                {/* Title tooltip */}
                <div
                  className="absolute bottom-0 left-0 right-0 px-2 py-1 text-[10px] truncate opacity-0 group-hover:opacity-100 transition-opacity duration-150 touch-visible"
                  style={{ background: "linear-gradient(to top, rgba(0,0,0,0.8), transparent)", color: C.textPrimary }}
                >
                  {d.title}
                </div>
              </motion.button>
            ))}
          </div>
        </div>
      )}

      {/* Other deliverables — expandable cards with preview */}
      {others.length > 0 && (
        <div className="space-y-1.5">
          {others.map((d) => (
            <DeliverableCard
              key={d.id}
              deliverable={d}
              boardId={boardId}
              taskId={taskId}
            />
          ))}
        </div>
      )}

      {/* Lightbox */}
      {lightbox && (
        <ImageLightbox src={lightbox.src} alt={lightbox.alt} onClose={() => setLightbox(null)} />
      )}
    </div>
  );
}

