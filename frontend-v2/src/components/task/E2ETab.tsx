"use client";

/**
 * E2ETab — human/agent E2E test result for a task (`task.e2e_test_required`).
 *
 * The tester agent documents its run as a TaskComment whose body contains
 * `**Result:** TEST_PASS` or `**Result:** TEST_FAIL` (see
 * backend/app/services/dispatch_message_builder.py::_build_test_message).
 * This tab surfaces that verdict as a badge, renders the comment body as the
 * flow protocol, and shows the screenshots/recording the tester attached as
 * TaskDeliverables — reusing the AuthImage/lightbox grid from
 * DeliverablesTab so screenshots don't get a second implementation.
 */

import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import ReactMarkdown from "react-markdown";
import { CheckCircle2, XCircle, Clock, Film, ZoomIn, X } from "lucide-react";
import { motion, AnimatePresence } from "framer-motion";
import { api, getToken } from "@/lib/api";
import { C, STATUS_TEXT } from "@/lib/colors";
import type { Task, TaskComment, TaskDeliverable } from "@/lib/types";

// ── Result-marker parsing ────────────────────────────────────────────────────

type E2EResult = "pass" | "fail" | null;

type ResultHit = { comment: TaskComment; result: "pass" | "fail" };

/** Newest comment carrying a `**Result:** TEST_PASS|TEST_FAIL` marker wins —
 *  a re-test after a fix supersedes the earlier verdict. */
function findResultComment(comments: TaskComment[]): ResultHit | null {
  const withResult: ResultHit[] = [];
  for (const c of comments) {
    if (/\*\*Result:\*\*\s*TEST_PASS/.test(c.content)) withResult.push({ comment: c, result: "pass" });
    else if (/\*\*Result:\*\*\s*TEST_FAIL/.test(c.content)) withResult.push({ comment: c, result: "fail" });
  }

  if (!withResult.length) return null;
  return withResult.reduce((latest, cur) =>
    new Date(cur.comment.created_at) > new Date(latest.comment.created_at) ? cur : latest
  );
}

// ── Badge ─────────────────────────────────────────────────────────────────

function ResultBadge({ result, status }: { result: E2EResult; status: Task["status"] }) {
  if (result === "pass") {
    return (
      <span
        className="inline-flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-xs font-medium"
        style={{ background: `${C.online}1F`, border: `1px solid ${C.online}55`, color: C.online }}
      >
        <CheckCircle2 size={13} /> E2E passed
      </span>
    );
  }
  if (result === "fail") {
    return (
      <span
        className="inline-flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-xs font-medium"
        style={{ background: `${C.error}1F`, border: `1px solid ${C.error}55`, color: STATUS_TEXT.error }}
      >
        <XCircle size={13} /> E2E failed
      </span>
    );
  }
  const running = status === "user_test";
  return (
    <span
      className="inline-flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-xs font-medium"
      style={{ background: "rgba(255,255,255,0.04)", border: `1px solid ${C.border}`, color: C.textMuted }}
    >
      <Clock size={13} /> {running ? "Test running" : "Awaiting test"}
    </span>
  );
}

// ── Authenticated media (shared fetch-as-blob pattern, DeliverablesTab has
//    the image half; this adds the video half) ──────────────────────────────

function AuthVideo({ src }: { src: string }) {
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
        className="w-full aspect-video rounded-xl flex items-center justify-center"
        style={{ background: "rgba(255,255,255,0.03)" }}
      >
        <div
          className="w-5 h-5 rounded-full border-2 border-t-transparent animate-spin"
          style={{ borderColor: `${C.accent}40`, borderTopColor: "transparent" }}
        />
      </div>
    );
  }

  // eslint-disable-next-line jsx-a11y/media-has-caption -- agent-recorded QA clips ship without captions
  return <video controls className="w-full rounded-xl" style={{ background: "#000" }} src={blobUrl} />;
}

function AuthImage({ src, alt, className, onClick }: { src: string; alt: string; className?: string; onClick?: () => void }) {
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
      });

    return () => {
      active = false;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [src]);

  if (failed) return null;
  if (!blobUrl) {
    return <div className={className} style={{ background: "rgba(255,255,255,0.03)" }} />;
  }
  return <img src={blobUrl} alt={alt} className={className} onClick={onClick} />;
}

function ImageLightbox({ src, alt, onClose }: { src: string; alt: string; onClose: () => void }) {
  return (
    <AnimatePresence>
      <motion.div
        key="e2e-lightbox"
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
          <div className="overflow-y-auto overflow-x-hidden rounded-xl" style={{ maxWidth: "90vw", maxHeight: "82vh" }}>
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
        </motion.div>
      </motion.div>
    </AnimatePresence>
  );
}

// ── Deliverable classification ───────────────────────────────────────────────

const VIDEO_EXT = /\.(webm|mp4)$/i;

function isVideoDeliverable(d: TaskDeliverable): boolean {
  // "video" ist der dedizierte Typ des Tester-Briefs; file/artifact bleiben
  // als Fallback fuer manuell registrierte Aufnahmen.
  if (d.deliverable_type === "video") return true;
  if (d.deliverable_type !== "file" && d.deliverable_type !== "artifact") return false;
  return VIDEO_EXT.test(d.path ?? "") || VIDEO_EXT.test(d.title ?? "");
}

function isScreenshotDeliverable(d: TaskDeliverable): boolean {
  return d.deliverable_type === "screenshot" && !!d.path;
}

// ── Tab ───────────────────────────────────────────────────────────────────

export function E2ETab({ task, boardId }: { task: Task; boardId: string }) {
  const [lightbox, setLightbox] = useState<{ src: string; alt: string } | null>(null);

  const { data: comments = [] } = useQuery({
    queryKey: ["task-comments", task.id],
    queryFn: () => api.tasks.comments.list(boardId, task.id),
  });

  const { data: deliverables = [] } = useQuery({
    queryKey: ["deliverables", boardId, task.id, "include_subtasks"],
    queryFn: () => api.tasks.deliverables.list(boardId, task.id, { includeSubtasks: true, depth: 2 }),
  });

  const resultInfo = useMemo(() => findResultComment(comments), [comments]);
  const screenshots = useMemo(() => deliverables.filter(isScreenshotDeliverable), [deliverables]);
  const videos = useMemo(() => deliverables.filter(isVideoDeliverable), [deliverables]);

  return (
    <div className="space-y-4">
      <ResultBadge result={resultInfo?.result ?? null} status={task.status} />

      {/* Flow protocol */}
      {resultInfo && (
        <div>
          <div className="text-[10px] font-semibold uppercase tracking-[0.07em] mb-2" style={{ color: C.textDim }}>
            Flow protocol
          </div>
          <div
            className="text-xs leading-relaxed prose prose-invert prose-xs max-w-none rounded-xl px-3 py-2.5"
            style={{ background: "rgba(255,255,255,0.02)", border: `1px solid ${C.border}`, color: C.textSecondary }}
          >
            <ReactMarkdown>{resultInfo.comment.content}</ReactMarkdown>
          </div>
        </div>
      )}

      {/* Screenshot gallery */}
      {screenshots.length > 0 && (
        <div>
          <div className="text-[10px] font-semibold uppercase tracking-[0.07em] mb-2" style={{ color: C.textDim }}>
            Screenshots ({screenshots.length})
          </div>
          <div className="grid gap-2" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(140px, 1fr))" }}>
            {screenshots.map((d, i) => {
              const src = `/api/v1/boards/${boardId}/tasks/${task.id}/deliverables/${d.id}/image`;
              return (
                <motion.button
                  key={d.id}
                  initial={{ opacity: 0, y: 4 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{ delay: i * 0.04, duration: 0.18, ease: "easeOut" }}
                  className="group relative rounded-xl overflow-hidden cursor-pointer"
                  style={{ aspectRatio: "16/10", background: "rgba(255,255,255,0.02)", border: `1px solid ${C.border}` }}
                  onClick={() => setLightbox({ src, alt: d.title })}
                  aria-label={`View screenshot: ${d.title}`}
                >
                  <AuthImage src={src} alt={d.title} className="w-full h-full object-cover transition-transform duration-200 group-hover:scale-[1.03]" />
                  <div
                    className="absolute inset-0 flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity duration-150"
                    style={{ background: "rgba(0,0,0,0.55)" }}
                  >
                    <ZoomIn size={18} style={{ color: "white" }} />
                  </div>
                </motion.button>
              );
            })}
          </div>
        </div>
      )}

      {/* Recording */}
      <div>
        <div className="text-[10px] font-semibold uppercase tracking-[0.07em] mb-2" style={{ color: C.textDim }}>
          Recording
        </div>
        {videos.length > 0 ? (
          <div className="space-y-3">
            {videos.map((d) => (
              <AuthVideo key={d.id} src={`/api/v1/boards/${boardId}/tasks/${task.id}/deliverables/${d.id}/file`} />
            ))}
          </div>
        ) : (
          <div
            className="flex flex-col items-center justify-center py-8 gap-2 rounded-xl"
            style={{ background: "rgba(255,255,255,0.02)", border: `1px solid ${C.border}` }}
          >
            <Film size={20} style={{ color: C.bgHover }} />
            <p className="text-xs" style={{ color: C.textMuted }}>No recording yet</p>
          </div>
        )}
      </div>

      {!resultInfo && screenshots.length === 0 && videos.length === 0 && (
        <p className="text-xs" style={{ color: C.textMuted }}>
          No E2E test evidence yet — the tester agent will attach a result comment, screenshots, and a recording once it runs.
        </p>
      )}

      {lightbox && <ImageLightbox src={lightbox.src} alt={lightbox.alt} onClose={() => setLightbox(null)} />}
    </div>
  );
}
