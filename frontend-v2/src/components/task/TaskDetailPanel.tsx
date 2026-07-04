"use client";

import { useState, useEffect } from "react";
import { useQuery, useQueryClient, useMutation } from "@tanstack/react-query";
import { motion } from "framer-motion";
import { useBodyScrollLock } from "@/hooks/useBodyScrollLock";
import { api } from "@/lib/api";
import { notify } from "@/lib/notify";
import { timeAgo } from "@/lib/utils";
import { C } from "@/lib/colors";
import { TaskHeader } from "./TaskHeader";
import { TaskDescription } from "./TaskDescription";
import { TaskContext } from "./TaskContext";
import { TaskActions } from "./TaskActions";
import { TaskComments } from "./TaskComments";
import { TaskHistory } from "./TaskHistory";
import { TaskTranscript } from "./TaskTranscript";
import { DeliverableCard } from "./DeliverableCard";
import {
  GitBranch, GitCommit, CheckSquare, Square, AlertCircle,
  ExternalLink, Image as ImageIcon,
  ChevronDown, X, ZoomIn,
} from "lucide-react";
import { AnimatePresence } from "framer-motion";
import type { Task, Agent, TaskEvent, TaskGitInfo, TaskChecklistItem, TaskDeliverable, CommitDiff } from "@/lib/types";
import { GitDiffView } from "@/components/git/GitDiffView";
import { getToken } from "@/lib/api";
import { useAppStore } from "@/lib/store";

// ── Operator-Briefing ────────────────────────────────────────────────────────

function OperatorBriefing({ task }: { task: Task }) {
  if (!task.intake_mode) return null;

  const fields: { label: string; value: string | null | undefined }[] = [
    { label: "Type", value: task.request_kind },
    { label: "Output", value: task.desired_output },
    { label: "Out of scope", value: task.scope_out },
    { label: "Risks", value: task.risk_notes },
    { label: "Criteria", value: task.acceptance_criteria },
    { label: "Browser", value: task.needs_browser ? "Yes" : null },
    { label: "Credentials", value: task.requires_auth ? "Yes" : null },
    { label: "Approval", value: task.approval_policy },
    { label: "Autonomy", value: task.autonomy_level },
    { label: "Links", value: task.reference_urls?.join(", ") || null },
    { label: "Notes", value: task.reference_notes },
  ];

  const visibleFields = fields.filter((f) => f.value);
  if (visibleFields.length === 0) return null;

  return (
    <div
      className="space-y-2 px-4 py-3 rounded-lg"
      style={{
        backgroundColor: "rgba(255, 255, 255, 0.02)",
        border: "1px solid rgba(255, 255, 255, 0.06)",
      }}
    >
      <div
        className="text-[10px] font-semibold uppercase tracking-[0.06em]"
        style={{ color: C.textMuted }}
      >
        Operator-Briefing ({task.intake_mode})
      </div>
      {visibleFields.map((f) => (
        <div key={f.label} className="text-xs">
          <span style={{ color: C.textMuted }}>{f.label}: </span>
          <span style={{ color: C.textPrimary }}>{f.value}</span>
        </div>
      ))}
    </div>
  );
}

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
          <AuthImage
            src={src}
            alt={alt}
            className="block rounded-xl object-contain max-w-[90vw] max-h-[85vh]"
            style={{ boxShadow: "0 0 60px rgba(0,0,0,0.8)" }}
          />
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

function DeliverablesTab({
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

// ── Commit Diff View ─────────────────────────────────────────────────────────

const MAX_LINES_PER_FILE = 500;

function CommitDiffView({ diff }: { diff: CommitDiff }) {
  const [collapsedFiles, setCollapsedFiles] = useState<Set<string>>(new Set());

  const toggleFile = (filename: string) =>
    setCollapsedFiles((s) => {
      const next = new Set(s);
      if (next.has(filename)) next.delete(filename);
      else next.add(filename);
      return next;
    });

  return (
    <div
      className="text-xs font-mono"
      style={{ borderTop: "1px solid rgba(255,255,255,0.04)" }}
    >
      {/* Stats bar */}
      <div
        className="flex items-center gap-3 px-4 py-1.5 text-[10px]"
        style={{ background: C.accentSubtle, borderBottom: `1px solid ${C.borderSubtle}`, color: C.textMuted }}
      >
        <span className="shrink-0 flex items-center gap-2">
          <span style={{ color: C.online }}>+{diff.stats.additions}</span>
          <span style={{ color: C.error }}>-{diff.stats.deletions}</span>
          <span>{diff.stats.files} {diff.stats.files === 1 ? "file" : "files"}</span>
        </span>
      </div>

      {/* Files */}
      {diff.files.map((file) => {
        const collapsed = collapsedFiles.has(file.filename);
        const allLines = file.hunks.flatMap((h) => h.lines);
        const truncated = allLines.length > MAX_LINES_PER_FILE;
        return (
          <div key={file.filename} style={{ borderBottom: "1px solid rgba(255,255,255,0.03)" }}>
            {/* File header */}
            <button
              onClick={() => toggleFile(file.filename)}
              className="w-full flex items-center gap-2 px-4 py-1 text-left transition-colors"
              style={{ borderLeft: `2px solid ${C.borderAccent}`, borderTop: "none", borderRight: "none", borderBottom: "none", background: C.accentSubtle, cursor: "pointer" }}
              onMouseEnter={(e) => { (e.currentTarget as HTMLElement).style.background = `${C.accent}10`; }}
              onMouseLeave={(e) => { (e.currentTarget as HTMLElement).style.background = C.accentSubtle; }}
            >
              <motion.span
                animate={{ rotate: collapsed ? -90 : 0 }}
                transition={{ duration: 0.15 }}
                style={{ color: C.textDim }}
              >
                <ChevronDown size={11} />
              </motion.span>
              <span className="flex-1 truncate text-[11px]" style={{ color: C.textPrimary }}>{file.filename}</span>
              <span className="shrink-0 flex items-center gap-1.5">
                {file.additions > 0 && (
                  <span className="px-1 rounded text-[10px]" style={{ background: `${C.online}1A`, color: C.online }}>
                    +{file.additions}
                  </span>
                )}
                {file.deletions > 0 && (
                  <span className="px-1 rounded text-[10px]" style={{ background: `${C.error}1A`, color: C.error }}>
                    -{file.deletions}
                  </span>
                )}
              </span>
            </button>

            {/* Diff lines */}
            <AnimatePresence initial={false}>
              {!collapsed && (
                <motion.div
                  initial={{ height: 0 }}
                  animate={{ height: "auto" }}
                  exit={{ height: 0 }}
                  transition={{ duration: 0.18, ease: "easeOut" }}
                  style={{ overflow: "hidden" }}
                >
                  <div style={{ overflowX: "auto", maxHeight: 400, overflowY: "auto" }}>
                    <table style={{ borderCollapse: "collapse", width: "100%", minWidth: "max-content" }}>
                      <tbody>
                        {file.hunks.map((hunk, hi) => {
                          const lines = truncated && hi === 0
                            ? hunk.lines.slice(0, MAX_LINES_PER_FILE)
                            : hunk.lines;
                          return (
                            <>
                              {/* Hunk header */}
                              <tr key={`hunk-${hi}`}>
                                <td colSpan={3} style={{ background: C.accentSubtle, color: C.textMuted, padding: "1px 8px", fontSize: 10 }}>
                                  {hunk.header}
                                </td>
                              </tr>
                              {lines.map((line, li) => {
                                const bg =
                                  line.type === "add" ? `${C.online}12` :
                                  line.type === "del" ? `${C.error}12` :
                                  "transparent";
                                const glyph =
                                  line.type === "add" ? "+" :
                                  line.type === "del" ? "-" : " ";
                                const glyphColor =
                                  line.type === "add" ? C.online :
                                  line.type === "del" ? C.error :
                                  "transparent";
                                const numColor = C.bgHover;
                                return (
                                  <tr key={`${hi}-${li}`} style={{ background: bg }}>
                                    <td
                                      style={{ padding: "0 8px", color: numColor, minWidth: 32, textAlign: "right", userSelect: "none", fontSize: 10, verticalAlign: "top", paddingTop: 1, paddingBottom: 1 }}
                                    >
                                      {line.old_no ?? ""}
                                    </td>
                                    <td
                                      style={{ padding: "0 8px", color: numColor, minWidth: 32, textAlign: "right", userSelect: "none", fontSize: 10, verticalAlign: "top", paddingTop: 1, paddingBottom: 1 }}
                                    >
                                      {line.new_no ?? ""}
                                    </td>
                                    <td
                                      style={{ padding: "1px 8px 1px 4px", color: glyphColor, userSelect: "none", fontSize: 10, verticalAlign: "top", paddingRight: 4 }}
                                    >
                                      {glyph}
                                    </td>
                                    <td
                                      style={{ padding: "1px 12px 1px 0", color: line.type === "ctx" ? C.textMuted : line.type === "add" ? `${C.online}CC` : `${C.error}CC`, whiteSpace: "pre", fontSize: 11 }}
                                    >
                                      {line.content}
                                    </td>
                                  </tr>
                                );
                              })}
                            </>
                          );
                        })}
                        {truncated && (
                          <tr>
                            <td colSpan={4} style={{ padding: "4px 12px", color: C.textMuted, fontSize: 10 }}>
                              … {allLines.length - MAX_LINES_PER_FILE} weitere Zeilen ausgeblendet
                            </td>
                          </tr>
                        )}
                      </tbody>
                    </table>
                  </div>
                </motion.div>
              )}
            </AnimatePresence>
          </div>
        );
      })}
    </div>
  );
}

// ── Git Panel ────────────────────────────────────────────────────────────────

function GitPanel({ gitInfo, boardId, taskId }: { gitInfo: TaskGitInfo; boardId: string; taskId: string }) {
  const [expanded, setExpanded] = useState(false);
  const [activeHash, setActiveHash] = useState<string | null>(null);
  const hasCommits = (gitInfo.commits?.length ?? 0) > 0;
  const repoUrl = gitInfo.repo_url ?? null;

  const { data: commitDiff, isFetching: diffLoading } = useQuery<CommitDiff>({
    queryKey: ["commit-diff", boardId, taskId, activeHash],
    queryFn: () => api.tasks.gitDiff(boardId, taskId, activeHash!),
    enabled: !!activeHash,
    staleTime: Infinity,
  });

  const handleCommitClick = (hash: string) =>
    setActiveHash((prev) => (prev === hash ? null : hash));

  const branchUrl = repoUrl && gitInfo.branch
    ? `${repoUrl}/tree/${gitInfo.branch}`
    : null;
  const commitUrl = (hash: string) =>
    repoUrl ? `${repoUrl}/commit/${hash}` : null;

  return (
    <div style={{ borderBottom: "1px solid rgba(255,255,255,0.06)" }}>
      {/* Header row */}
      <div
        className="flex items-center gap-2 px-4 py-2 text-xs"
        style={{ color: C.textSecondary }}
      >
        {/* Branch */}
        <span className="flex items-center gap-1.5 shrink-0 min-w-0">
          <GitBranch size={12} style={{ color: C.accent }} />
          {branchUrl ? (
            <a
              href={branchUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="font-medium hover:underline transition-opacity hover:opacity-80 truncate"
              style={{ color: C.textPrimary, maxWidth: 160 }}
              title={gitInfo.branch ?? ""}
            >
              {gitInfo.branch}
            </a>
          ) : (
            <span className="font-medium truncate" style={{ color: C.textPrimary, maxWidth: 160 }}>{gitInfo.branch}</span>
          )}
        </span>

        {/* Ahead indicator */}
        {gitInfo.ahead > 0 && (
          <span className="flex items-center gap-1 shrink-0" style={{ color: C.textMuted }}>
            <GitCommit size={10} />
            <span className="text-[10px]">{gitInfo.ahead} ahead</span>
          </span>
        )}

        {/* Uncommitted badge */}
        {gitInfo.uncommitted && (
          <span
            className="px-1.5 py-0.5 rounded text-[10px] font-medium shrink-0"
            style={{ background: `${C.warning}1A`, color: C.warning, border: `1px solid ${C.warning}33` }}
          >
            uncommitted
          </span>
        )}

        <span className="flex items-center gap-1.5 ml-auto shrink-0">
          {/* Repo chip */}
          {repoUrl && gitInfo.repo_name && (
            <a
              href={repoUrl}
              target="_blank"
              rel="noopener noreferrer"
              onClick={(e) => e.stopPropagation()}
              className="flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-medium hover:opacity-80 transition-opacity"
              style={{
                background: C.bgElevated,
                color: C.textMuted,
                border: `1px solid ${C.border}`,
                fontFamily: "var(--font-geist-mono), monospace",
              }}
              title={repoUrl}
            >
              <svg width="10" height="10" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">
                <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"/>
              </svg>
              {gitInfo.repo_name}
            </a>
          )}

          {/* PR link */}
          {gitInfo.pr_url && (
            <a
              href={gitInfo.pr_url}
              target="_blank"
              rel="noopener noreferrer"
              onClick={(e) => e.stopPropagation()}
              className="flex items-center gap-1 px-2 py-0.5 rounded text-[10px] font-medium cursor-pointer hover:opacity-80 transition-opacity"
              style={{
                background: C.accentSubtle,
                color: C.accent,
                border: `1px solid ${C.borderAccent}`,
              }}
            >
              <ExternalLink size={9} />
              PR open
            </a>
          )}

          {/* Expand commits */}
          {hasCommits && (
            <button
              onClick={() => setExpanded((x) => !x)}
              className="flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] transition-colors"
              style={{
                background: expanded ? C.accentSubtle : "transparent",
                color: expanded ? C.accent : C.textDim,
                border: `1px solid ${expanded ? C.borderAccent : C.border}`,
                cursor: "pointer",
              }}
              aria-expanded={expanded}
            >
              <GitCommit size={10} />
              <span>{gitInfo.commits!.length}</span>
              <motion.span
                animate={{ rotate: expanded ? 180 : 0 }}
                transition={{ duration: 0.15 }}
                style={{ display: "block" }}
              >
                <ChevronDown size={10} />
              </motion.span>
            </button>
          )}
        </span>
      </div>

      {/* Commit list */}
      <AnimatePresence>
        {expanded && hasCommits && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.2, ease: "easeOut" }}
            style={{ overflow: "hidden" }}
          >
            <div
              className="pb-2"
              style={{ borderTop: "1px solid rgba(255,255,255,0.04)" }}
            >
              {gitInfo.commits!.map((commit, i) => {
                const isActive = activeHash === commit.hash;
                const isLoading = diffLoading && isActive;
                const ghUrl = commitUrl(commit.hash);
                return (
                  <div key={commit.hash}>
                    <motion.button
                      initial={{ opacity: 0, x: -4 }}
                      animate={{ opacity: 1, x: 0 }}
                      transition={{ delay: i * 0.03, duration: 0.15, ease: "easeOut" }}
                      onClick={() => handleCommitClick(commit.hash)}
                      className="w-full flex items-start gap-3 px-4 py-2 text-left transition-colors"
                      style={{
                        background: isActive ? C.accentSubtle : "transparent",
                        border: "none",
                        cursor: "pointer",
                        borderLeft: isActive ? `2px solid ${C.borderAccent}` : "2px solid transparent",
                      }}
                      onMouseEnter={(e) => { if (!isActive) (e.currentTarget as HTMLElement).style.background = "rgba(255,255,255,0.02)"; }}
                      onMouseLeave={(e) => { if (!isActive) (e.currentTarget as HTMLElement).style.background = "transparent"; }}
                    >
                      {/* Timeline dot */}
                      <div className="flex flex-col items-center shrink-0 mt-1.5">
                        <div
                          className="w-1.5 h-1.5 rounded-full shrink-0"
                          style={{ background: isActive ? C.accent : i === 0 ? C.accent : C.bgHover }}
                        />
                        {i < gitInfo.commits!.length - 1 && (
                          <div className="w-px flex-1 mt-1" style={{ background: "rgba(255,255,255,0.05)", minHeight: 12 }} />
                        )}
                      </div>

                      {/* Content */}
                      <div className="flex-1 min-w-0 pb-0.5">
                        <div className="flex items-baseline gap-2">
                          {/* Hash — clickable to GitHub if available */}
                          {ghUrl ? (
                            <a
                              href={ghUrl}
                              target="_blank"
                              rel="noopener noreferrer"
                              onClick={(e) => e.stopPropagation()}
                              className="text-[10px] font-mono shrink-0 hover:underline transition-opacity hover:opacity-80"
                              style={{ color: i === 0 ? C.accent : C.textMuted }}
                              title={`Open commit ${commit.hash} on GitHub`}
                            >
                              {commit.hash}
                            </a>
                          ) : (
                            <span
                              className="text-[10px] font-mono shrink-0"
                              style={{ color: i === 0 ? C.accent : C.textDim }}
                            >
                              {commit.hash}
                            </span>
                          )}
                          <span
                            className="text-xs truncate"
                            style={{ color: i === 0 ? C.textPrimary : C.textMuted }}
                          >
                            {commit.message}
                          </span>
                        </div>
                        <div className="flex items-center gap-1.5 mt-0.5 text-[10px]" style={{ color: C.bgHover }}>
                          <span>{commit.author}</span>
                          <span>·</span>
                          <span>{commit.date}</span>
                        </div>
                      </div>

                      {/* Loading / expand indicator */}
                      <div className="shrink-0 mt-1.5">
                        {isLoading ? (
                          <motion.div
                            animate={{ rotate: 360 }}
                            transition={{ repeat: Infinity, duration: 0.8, ease: "linear" }}
                            style={{ width: 10, height: 10, borderRadius: "50%", border: `1.5px solid ${C.accentSubtle}`, borderTopColor: C.accent }}
                          />
                        ) : (
                          <motion.span
                            animate={{ rotate: isActive ? 180 : 0 }}
                            transition={{ duration: 0.15 }}
                            style={{ color: isActive ? C.accent : C.bgHover, display: "block" }}
                          >
                            <ChevronDown size={10} />
                          </motion.span>
                        )}
                      </div>
                    </motion.button>

                    {/* Inline diff */}
                    <AnimatePresence>
                      {isActive && commitDiff && !diffLoading && (
                        <motion.div
                          initial={{ height: 0, opacity: 0 }}
                          animate={{ height: "auto", opacity: 1 }}
                          exit={{ height: 0, opacity: 0 }}
                          transition={{ duration: 0.2, ease: "easeOut" }}
                          style={{ overflow: "hidden" }}
                        >
                          <GitDiffView diff={commitDiff} />
                        </motion.div>
                      )}
                    </AnimatePresence>
                  </div>
                );
              })}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

// ── Tab Button ──────────────────────────────────────────────────────────────

function TabButton({
  active,
  label,
  onClick,
}: {
  active: boolean;
  label: string;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      className="px-2.5 py-1 rounded-lg text-xs font-medium transition-all cursor-pointer"
      style={{
        backgroundColor: active ? C.accent : "rgba(255, 255, 255, 0.03)",
        color: active ? C.textPrimary : C.textMuted,
        border: `1px solid ${active ? C.borderAccent : C.border}`,
      }}
    >
      {label}
    </button>
  );
}

// ── TaskDetailPanel ──────────────────────────────────────────────────────────

interface TaskDetailPanelProps {
  task: Task;
  agents: Agent[];
  boardId: string;
  onClose: () => void;
  variant?: "modal" | "panel";
}

export default function TaskDetailPanel({
  task,
  agents,
  boardId,
  onClose,
  variant,
}: TaskDetailPanelProps) {
  const qc = useQueryClient();
  const [confirmDelete, setConfirmDelete] = useState(false);

  // iOS-safe scroll lock — only in modal variant (M4); panel variant is embedded in layout
  useBodyScrollLock(variant === "modal");
  const [activeTab, setActiveTab] = useState<"comments" | "history" | "transcript" | "deliverables">("comments");

  // ── Mutations ──────────────────────────────────────────────────────────────

  const updateMutation = useMutation({
    mutationFn: (data: Partial<Task>) => api.tasks.update(boardId, task.id, data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["tasks", boardId] });
      qc.invalidateQueries({ queryKey: ["pipeline", boardId] });
      qc.invalidateQueries({ queryKey: ["task", boardId, task.id] });
    },
  });

  const deleteMutation = useMutation({
    mutationFn: () => api.tasks.delete(boardId, task.id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["tasks", boardId] });
      qc.invalidateQueries({ queryKey: ["pipeline", boardId] });
      onClose();
    },
    onError: (e: Error) => notify.error(`Delete failed: ${e.message}`),
  });

  // ── Data queries ──────────────────────────────────────────────────────────

  const { data: events, isLoading: isEventsLoading } = useQuery({
    queryKey: ["task-events", task.id],
    queryFn: () => api.tasks.events(boardId, task.id),
    enabled: activeTab === "history",
  });

  const { data: deliverables } = useQuery({
    queryKey: ["deliverables", boardId, task.id, "include_subtasks"],
    // include_subtasks=true also shows subtask deliverables — important for
    // orchestrator parent tasks (boss delegation) so the operator sees the full
    // output tree without opening each subtask individually. Grouping
    // happens client-side via source_depth / source_task_title.
    queryFn: () =>
      api.tasks.deliverables.list(boardId, task.id, { includeSubtasks: true, depth: 2 }),
    enabled: activeTab === "deliverables",
  });

  // Git info (only when workspace_path is set)
  const { data: gitInfo } = useQuery<TaskGitInfo>({
    queryKey: ["task-git-info", boardId, task.id],
    queryFn: () => api.tasks.gitInfo(boardId, task.id),
    enabled: !!task.workspace_path,
    refetchInterval: 30_000,
  });

  // Checklist
  const { data: checklist = [] } = useQuery<TaskChecklistItem[]>({
    queryKey: ["task-checklist", boardId, task.id],
    queryFn: () => api.tasks.checklist.list(boardId, task.id),
    refetchInterval: 15_000,
  });

  const agent = agents.find((a) => a.id === task.assigned_agent_id);
  const currentUser = useAppStore((s) => s.currentUser);

  // Creator name: currentUser if ID matches, otherwise fetch the user list
  const { data: usersList } = useQuery({
    queryKey: ["users-list"],
    queryFn: () => api.auth.users.list(),
    enabled: !!task.created_by_user_id && task.created_by_user_id !== currentUser?.id,
    staleTime: 60_000,
  });
  const creatorName = task.created_by_user_id
    ? task.created_by_user_id === currentUser?.id
      ? currentUser.name
      : usersList?.find((u) => u.id === task.created_by_user_id)?.name ?? "User"
    : null;

  // Tab config
  const tabs: { key: "comments" | "history" | "transcript" | "deliverables"; label: string }[] = [
    { key: "comments", label: "Comments" },
    { key: "history", label: "History" },
    ...(task.spawn_session_key || task.dispatched_at
      ? [{ key: "transcript" as const, label: "Transcript" }]
      : []),
    { key: "deliverables", label: "Deliverables" },
  ];

  if (variant === "panel") {
    return (
      <motion.div
        key={task.id}
        initial={{ opacity: 0, x: 24 }}
        animate={{ opacity: 1, x: 0 }}
        exit={{ opacity: 0, x: 24 }}
        transition={{ duration: 0.2, ease: [0.16, 1, 0.3, 1] }}
        className="w-[420px] shrink-0 border-l flex flex-col overflow-hidden"
        style={{
          borderColor: C.borderActive,
          backgroundColor: C.bgBase,
        }}
      >
        <TaskHeader
          task={task}
          agent={agent}
          confirmDelete={confirmDelete}
          setConfirmDelete={setConfirmDelete}
          onDelete={() => deleteMutation.mutate()}
          deleteLoading={deleteMutation.isPending}
          onClose={onClose}
        />
        <div className="flex-1 overflow-y-auto space-y-4">
          {gitInfo?.branch && <GitPanel gitInfo={gitInfo} boardId={boardId} taskId={task.id} />}
          {/* Checklist */}
          {checklist.length > 0 && (
            <div
              className="px-4 py-3 border-b"
              style={{ borderColor: "rgba(255,255,255,0.06)" }}
            >
              {/* Progress Bar */}
              <div className="flex items-center gap-2 mb-2">
                <div
                  className="flex-1 h-1.5 rounded-full"
                  style={{ background: "rgba(255,255,255,0.08)" }}
                >
                  <div
                    className="h-full rounded-full transition-all"
                    style={{
                      width: `${
                        task.checklist_total > 0
                          ? (task.checklist_done / task.checklist_total) * 100
                          : 0
                      }%`,
                      background: C.accent,
                    }}
                  />
                </div>
                <span className="text-xs" style={{ color: C.textSecondary }}>
                  {task.checklist_done}/{task.checklist_total}
                </span>
              </div>
              {/* Items */}
              <div className="space-y-1">
                {checklist.map((item) => (
                  <div key={item.id} className="flex items-center gap-2 text-xs">
                    {item.status === "done" ? (
                      <CheckSquare
                        size={12}
                        style={{ color: C.online, flexShrink: 0 }}
                      />
                    ) : item.status === "blocked" ? (
                      <AlertCircle
                        size={12}
                        style={{ color: C.error, flexShrink: 0 }}
                      />
                    ) : (
                      <Square size={12} style={{ color: C.textMuted, flexShrink: 0 }} />
                    )}
                    <span
                      style={{
                        color: item.status === "done" ? C.textMuted : C.textPrimary,
                        textDecoration:
                          item.status === "done" ? "line-through" : "none",
                      }}
                    >
                      {item.title}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Description */}
          {task.description && (
            <TaskDescription description={task.description} />
          )}

          {/* Operator Briefing */}
          {task.intake_mode && (
            <div className="px-4">
              <OperatorBriefing task={task} />
            </div>
          )}

          {/* Context & Dependencies */}
          <div className="px-4">
            <TaskContext
              task={task}
              agents={agents}
              boardId={boardId}
              onAssign={(agentId) =>
                updateMutation.mutate({ assigned_agent_id: agentId } as Partial<Task>)
              }
              onAssignProject={(projectId) =>
                updateMutation.mutate({ project_id: projectId } as Partial<Task>)
              }
            />
          </div>

          {/* Actions (promote, stop/resume, review, status change) */}
          <div className="px-4">
            <TaskActions task={task} boardId={boardId} />
          </div>

          {/* Meta row: Started + Created by */}
          <div className="px-4 flex items-center gap-3 text-xs" style={{ color: C.textMuted }}>
            {task.started_at && (
              <span>Started {timeAgo(task.started_at)}</span>
            )}
            {creatorName && (
              <span style={{ color: C.textMuted }}>
                Erstellt von{" "}
                <span style={{ color: C.textSecondary }}>{creatorName}</span>
              </span>
            )}
          </div>

          {/* Tabs */}
          <div className="px-4 pb-4">
            {/* Tab bar */}
            <div className="flex gap-2 mb-3">
              {tabs.map((tab) => (
                <TabButton
                  key={tab.key}
                  active={activeTab === tab.key}
                  label={tab.label}
                  onClick={() => setActiveTab(tab.key)}
                />
              ))}
            </div>

            {/* Tab content */}
            {activeTab === "comments" ? (
              <TaskComments
                task={task}
                boardId={boardId}
                agents={agents}
              />
            ) : activeTab === "transcript" ? (
              <TaskTranscript
                taskId={task.id}
                isLive={task.status === "in_progress" || task.status === "review"}
              />
            ) : activeTab === "deliverables" ? (
              <DeliverablesTab
                deliverables={deliverables ?? []}
                boardId={boardId}
                taskId={task.id}
              />
            ) : (
              <TaskHistory
                events={(events as TaskEvent[]) ?? []}
                isLoading={isEventsLoading}
              />
            )}
          </div>
        </div>
      </motion.div>
    );
  }

  return (
    <>
      {/* Backdrop overlay */}
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        transition={{ duration: 0.15 }}
        className="fixed inset-0 z-50 flex items-end sm:items-center justify-center sm:p-8"
        style={{
          backgroundColor: "rgba(0, 0, 0, 0.6)",
          backdropFilter: "blur(8px)",
          WebkitBackdropFilter: "blur(8px)",
          paddingTop: "calc(env(safe-area-inset-top) + 3.5rem)",
          paddingBottom: "env(safe-area-inset-bottom)",
          paddingLeft: "env(safe-area-inset-left)",
          paddingRight: "env(safe-area-inset-right)",
          touchAction: "none",
        }}
        onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}
      >

      {/* Centered Panel */}
      <motion.div
        initial={{ opacity: 0, y: "100%" }}
        animate={{ opacity: 1, y: 0 }}
        exit={{ opacity: 0, y: "100%" }}
        transition={{ duration: 0.28, ease: [0.16, 1, 0.3, 1] }}
        className="relative w-full rounded-t-2xl sm:rounded-2xl sm:max-w-2xl flex flex-col z-[51] overflow-hidden"
        role="dialog"
        aria-modal="true"
        aria-label="Task Details"
        style={{
          maxHeight: "calc(100dvh - env(safe-area-inset-top) - 5.5rem)",
          backgroundColor: C.bgBase,
          border: `1px solid ${C.borderActive}`,
          boxShadow: "0 4px 24px rgba(0,0,0,0.5), 0 1px 2px rgba(0,0,0,0.3)",
        }}
        onClick={(e) => e.stopPropagation()}
      >
        {/* Drag handle */}
        <div className="sm:hidden flex justify-center pt-2 pb-1 shrink-0">
          <div className="w-9 h-1 rounded-full" style={{ backgroundColor: "rgba(255,255,255,0.15)" }} />
        </div>
        {/* Top edge highlight */}
        <div className="absolute top-0 left-0 right-0 h-px z-10" style={{ background: "linear-gradient(90deg, transparent, rgba(255,255,255,0.12), transparent)" }} />
        {/* Header */}
        <TaskHeader
          task={task}
          agent={agent}
          confirmDelete={confirmDelete}
          setConfirmDelete={setConfirmDelete}
          onDelete={() => deleteMutation.mutate()}
          deleteLoading={deleteMutation.isPending}
          onClose={onClose}
        />

        {/* Body */}
        <div className="flex-1 overflow-y-auto space-y-4" style={{ overscrollBehavior: "contain", WebkitOverflowScrolling: "touch" } as React.CSSProperties}>

        {/* Git Panel — inside scroll so diffs can expand naturally */}
        {gitInfo?.branch && <GitPanel gitInfo={gitInfo} boardId={boardId} taskId={task.id} />}
          {/* Checklist */}
          {checklist.length > 0 && (
            <div
              className="px-4 py-3 border-b"
              style={{ borderColor: "rgba(255,255,255,0.06)" }}
            >
              {/* Progress Bar */}
              <div className="flex items-center gap-2 mb-2">
                <div
                  className="flex-1 h-1.5 rounded-full"
                  style={{ background: "rgba(255,255,255,0.08)" }}
                >
                  <div
                    className="h-full rounded-full transition-all"
                    style={{
                      width: `${
                        task.checklist_total > 0
                          ? (task.checklist_done / task.checklist_total) * 100
                          : 0
                      }%`,
                      background: C.accent,
                    }}
                  />
                </div>
                <span className="text-xs" style={{ color: C.textSecondary }}>
                  {task.checklist_done}/{task.checklist_total}
                </span>
              </div>
              {/* Items */}
              <div className="space-y-1">
                {checklist.map((item) => (
                  <div key={item.id} className="flex items-center gap-2 text-xs">
                    {item.status === "done" ? (
                      <CheckSquare
                        size={12}
                        style={{ color: C.online, flexShrink: 0 }}
                      />
                    ) : item.status === "blocked" ? (
                      <AlertCircle
                        size={12}
                        style={{ color: C.error, flexShrink: 0 }}
                      />
                    ) : (
                      <Square size={12} style={{ color: C.textMuted, flexShrink: 0 }} />
                    )}
                    <span
                      style={{
                        color: item.status === "done" ? C.textMuted : C.textPrimary,
                        textDecoration:
                          item.status === "done" ? "line-through" : "none",
                      }}
                    >
                      {item.title}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Description */}
          {task.description && (
            <TaskDescription description={task.description} />
          )}

          {/* Operator Briefing */}
          {task.intake_mode && (
            <div className="px-4">
              <OperatorBriefing task={task} />
            </div>
          )}

          {/* Context & Dependencies */}
          <div className="px-4">
            <TaskContext
              task={task}
              agents={agents}
              boardId={boardId}
              onAssign={(agentId) =>
                updateMutation.mutate({ assigned_agent_id: agentId } as Partial<Task>)
              }
              onAssignProject={(projectId) =>
                updateMutation.mutate({ project_id: projectId } as Partial<Task>)
              }
            />
          </div>

          {/* Actions (promote, stop/resume, review, status change) */}
          <div className="px-4">
            <TaskActions task={task} boardId={boardId} />
          </div>

          {/* Meta row: Started + Created by */}
          <div className="px-4 flex items-center gap-3 text-xs" style={{ color: C.textMuted }}>
            {task.started_at && (
              <span>Started {timeAgo(task.started_at)}</span>
            )}
            {creatorName && (
              <span style={{ color: C.textMuted }}>
                Erstellt von{" "}
                <span style={{ color: C.textSecondary }}>{creatorName}</span>
              </span>
            )}
          </div>

          {/* Tabs */}
          <div className="px-4 pb-4">
            {/* Tab bar */}
            <div className="flex gap-2 mb-3">
              {tabs.map((tab) => (
                <TabButton
                  key={tab.key}
                  active={activeTab === tab.key}
                  label={tab.label}
                  onClick={() => setActiveTab(tab.key)}
                />
              ))}
            </div>

            {/* Tab content */}
            {activeTab === "comments" ? (
              <TaskComments
                task={task}
                boardId={boardId}
                agents={agents}
              />
            ) : activeTab === "transcript" ? (
              <TaskTranscript
                taskId={task.id}
                isLive={task.status === "in_progress" || task.status === "review"}
              />
            ) : activeTab === "deliverables" ? (
              <DeliverablesTab
                deliverables={deliverables ?? []}
                boardId={boardId}
                taskId={task.id}
              />
            ) : (
              <TaskHistory
                events={(events as TaskEvent[]) ?? []}
                isLoading={isEventsLoading}
              />
            )}
          </div>
        </div>
      </motion.div>
      </motion.div>
    </>
  );
}
