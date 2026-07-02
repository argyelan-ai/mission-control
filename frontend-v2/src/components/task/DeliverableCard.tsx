"use client";

import { useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import ReactMarkdown from "react-markdown";
import {
  FileText, Package, Link2, ChevronDown, ChevronUp,
  ExternalLink, FolderOpen,
} from "lucide-react";
import { api } from "@/lib/api";
import type { TaskDeliverable } from "@/lib/types";
import { FilePreview } from "./FilePreview";
import { DirectoryBrowser } from "./DirectoryBrowser";
import { C } from "@/lib/colors";

function DeliverableIcon({ type }: { type: string }) {
  const map: Record<string, React.ReactNode> = {
    file: <FileText size={12} />,
    document: <FileText size={12} />,
    url: <Link2 size={12} />,
    artifact: <Package size={12} />,
  };
  return <>{map[type] ?? <Package size={12} />}</>;
}

function isDirectory(path: string | null): boolean {
  if (!path) return false;
  const last = path.split("/").pop() ?? "";
  return !last.includes(".");
}

interface DeliverableCardProps {
  deliverable: TaskDeliverable;
  boardId: string;
  taskId: string;
}

export function DeliverableCard({ deliverable: d, boardId, taskId }: DeliverableCardProps) {
  const [expanded, setExpanded] = useState(false);
  // Finder is a macOS bonus: render best-effort and hide once the backend
  // reports it isn't available here (409 container-only / 501 mobile/remote).
  // The viewer + download always work regardless — no path-prefix guessing.
  const [finderAvailable, setFinderAvailable] = useState(true);

  const isUrl = d.deliverable_type === "url";
  const hasContent = !!d.content;
  const isDir = isDirectory(d.path);

  // Build the content URL for any file-backed deliverable. The backend decides
  // reachability; FilePreview surfaces a graceful error if the bytes aren't
  // available (e.g. container-only paths) instead of us guessing by prefix.
  const fileUrl = d.path && !isUrl
    ? `/api/v1/boards/${boardId}/tasks/${taskId}/deliverables/${d.id}/file`
    : null;

  const canExpand = hasContent || (!!d.path && !isUrl);

  async function reveal(subpath?: string) {
    try {
      await api.tasks.deliverables.open(boardId, taskId, d.id, { reveal: true, subpath });
    } catch {
      setFinderAvailable(false); // hide the button — not available here
    }
  }

  async function openWithApp(e: React.MouseEvent) {
    e.stopPropagation();
    if (!d.path || isUrl) return;
    try {
      await api.tasks.deliverables.open(boardId, taskId, d.id, { reveal: false });
    } catch {
      setFinderAvailable(false);
    }
  }

  const showFinder = !!d.path && !isUrl && finderAvailable;

  return (
    <motion.div
      initial={{ opacity: 0, x: -4 }}
      animate={{ opacity: 1, x: 0 }}
      transition={{ duration: 0.18, ease: "easeOut" }}
      style={{
        background: expanded ? C.accentSubtle : "rgba(255,255,255,0.02)",
        border: expanded ? `1px solid ${C.borderAccent}` : `1px solid ${C.border}`,
        borderRadius: 12,
        overflow: "hidden",
        transition: "background 0.15s, border-color 0.15s",
      }}
    >
      {/* Card Header */}
      <div
        className={`flex items-start gap-2.5 px-3 py-2.5 ${canExpand ? "cursor-pointer" : ""}`}
        onClick={() => canExpand && setExpanded((v) => !v)}
        role={canExpand ? "button" : undefined}
        tabIndex={canExpand ? 0 : undefined}
        onKeyDown={(e) => canExpand && e.key === "Enter" && setExpanded((v) => !v)}
      >
        <span className="mt-0.5 shrink-0" style={{ color: C.textMuted }}>
          <DeliverableIcon type={d.deliverable_type} />
        </span>

        <div className="flex-1 min-w-0">
          {/* Title + Type Badge */}
          <div className="flex items-center gap-2">
            <span className="text-xs font-medium" style={{ color: C.textPrimary }}>{d.title}</span>
            <span
              className="text-[9px] px-1.5 py-0.5 rounded font-mono uppercase tracking-wider shrink-0"
              style={{ background: C.bgElevated, color: C.textMuted }}
            >
              {d.deliverable_type}
            </span>
            {hasContent && (
              <span
                className="text-[9px] px-1.5 py-0.5 rounded shrink-0"
                style={{ background: `${C.online}1A`, color: C.online }}
              >
                content
              </span>
            )}
            {/* Source-Badge: nur wenn Deliverable aus Subtask stammt (source_depth > 0).
                Zeigt dem Reviewer woher der Output kommt ohne extra Navigation. */}
            {(d.source_depth ?? 0) > 0 && d.source_task_title && (
              <span
                className="text-[9px] px-1.5 py-0.5 rounded shrink-0 truncate"
                title={`Von Subtask: ${d.source_task_title}`}
                style={{
                  background: C.accentSubtle,
                  color: C.accent,
                  maxWidth: "220px",
                }}
              >
                ← {d.source_task_title}
              </span>
            )}
          </div>

          {/* Description */}
          {d.description && (
            <p className="text-xs mt-0.5 truncate" style={{ color: C.textMuted }}>{d.description}</p>
          )}

          {/* Path — URL */}
          {d.path && isUrl && (
            <a
              href={d.path}
              target="_blank"
              rel="noopener noreferrer"
              className="flex items-center gap-1 mt-1 text-xs hover:underline cursor-pointer"
              style={{ color: C.accent }}
              onClick={(e) => e.stopPropagation()}
            >
              <ExternalLink size={10} />
              <span className="truncate">{d.path}</span>
            </a>
          )}

          {/* Path — file path. Clickable to reveal in Finder when available,
              otherwise plain text. No prefix guessing — the click is best-effort. */}
          {d.path && !isUrl && (
            showFinder ? (
              <button
                onClick={(e) => { e.stopPropagation(); reveal(); }}
                className="flex items-center gap-1 mt-0.5 text-left hover:underline cursor-pointer"
                title="Im Finder anzeigen"
              >
                <span className="text-[10px] font-mono truncate" style={{ color: C.textMuted }}>{d.path}</span>
              </button>
            ) : (
              <div className="flex items-center gap-1 mt-0.5">
                <span className="text-[10px] font-mono truncate" style={{ color: C.textMuted }}>{d.path}</span>
              </div>
            )
          )}

          {/* Agent + Date */}
          <div className="flex items-center gap-2 mt-1 text-[10px]" style={{ color: C.textMuted }}>
            {d.agent_name && <span>{d.agent_name}</span>}
            <span>{new Date(d.created_at).toLocaleString("de-CH", { dateStyle: "short", timeStyle: "short" })}</span>
          </div>
        </div>

        {/* Right controls */}
        <div className="flex items-center gap-2 shrink-0 mt-0.5">
          {showFinder && (
            <button
              onClick={openWithApp}
              className="text-[10px] cursor-pointer hover:underline shrink-0"
              style={{ color: C.accent }}
              title="Mit Standard-App öffnen"
              aria-label="Mit Standard-App öffnen"
            >
              <FolderOpen size={12} />
            </button>
          )}
          {canExpand && (
            <span style={{ color: C.textMuted }}>
              {expanded ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
            </span>
          )}
        </div>
      </div>

      {/* Expanded Preview */}
      <AnimatePresence>
        {expanded && canExpand && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.2, ease: [0.16, 1, 0.3, 1] }}
            style={{ overflow: "hidden" }}
          >
            <div
              className="px-3 pb-3"
              style={{ borderTop: `1px solid ${C.borderSubtle}` }}
            >
              <div className="pt-2">
                {/* Priorität 1: Inline-Content (Markdown) */}
                {hasContent ? (
                  <div
                    className="text-xs leading-relaxed prose prose-invert prose-xs max-w-none overflow-y-auto"
                    style={{ maxHeight: 500, color: C.textSecondary }}
                  >
                    <ReactMarkdown>{d.content!}</ReactMarkdown>
                  </div>
                ) : isDir ? (
                  <DirectoryBrowser
                    boardId={boardId}
                    taskId={taskId}
                    deliverableId={d.id}
                    onOpenInFinder={showFinder ? reveal : undefined}
                  />
                ) : fileUrl && d.path ? (
                  <FilePreview fileUrl={fileUrl} path={d.path} />
                ) : null}
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  );
}
