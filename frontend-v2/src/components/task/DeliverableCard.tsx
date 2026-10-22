"use client";

import { useEffect, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import ReactMarkdown from "react-markdown";
import { useTranslations } from "next-intl";
import {
  FileText, Package, Link2, ChevronDown, ChevronUp,
  ExternalLink, FolderOpen, FolderSearch,
} from "lucide-react";
import { api } from "@/lib/api";
import type { TaskDeliverable } from "@/lib/types";
import { FilePreview } from "./FilePreview";
import { DirectoryBrowser } from "./DirectoryBrowser";
import { C } from "@/lib/colors";
import { HOST_ONLY_CLASS } from "@/lib/hostOnly";

function DeliverableIcon({ type }: { type: string }) {
  const map: Record<string, React.ReactNode> = {
    file: <FileText size={14} />,
    document: <FileText size={14} />,
    url: <Link2 size={14} />,
    artifact: <Package size={14} />,
  };
  return <>{map[type] ?? <Package size={14} />}</>;
}

function isDirectory(path: string | null): boolean {
  if (!path) return false;
  const last = path.split("/").pop() ?? "";
  return !last.includes(".");
}

/** One word for what the operator gets: the file's format (HTML, PDF),
 *  "Folder", "Link" or "Text" — never the internal type ("file",
 *  "document") next to a second "content" badge (round-1 user test r2 S6). */
function useKindLabel(d: TaskDeliverable): string {
  const t = useTranslations("deliverableCard");
  if (d.deliverable_type === "url") return t("kind.link");
  if (d.path) {
    if (isDirectory(d.path)) return t("kind.folder");
    const ext = (d.path.split("/").pop() ?? "").split(".").pop() ?? "";
    return /^[a-z0-9]{1,5}$/i.test(ext) ? ext.toUpperCase() : t("kind.file");
  }
  return d.content ? t("kind.text") : t("kind.other");
}

/** What the last Mac action did, shown next to the buttons. */
type HostNote = "opened" | "shown" | "unavailable";
const HOST_NOTE_KEY: Record<HostNote, string> = {
  opened: "openedOnMac",
  shown: "shownInFinder",
  unavailable: "hostUnavailable",
};

interface DeliverableCardProps {
  deliverable: TaskDeliverable;
  boardId: string;
  taskId: string;
}

export function DeliverableCard({ deliverable: d, boardId, taskId }: DeliverableCardProps) {
  const t = useTranslations("deliverableCard");
  const [expanded, setExpanded] = useState(false);
  // "Open on the Mac" / "Show in Finder" act on the Mac MC runs on, not on
  // the device in hand. So they sit in the expanded card, away from the
  // arrow, in words, at a desktop with a mouse only (HOST_ONLY_CLASS), and
  // say what happened. They disappear once the backend reports that it
  // cannot open files here (409 container-only / 501 not a Mac).
  const [hostAvailable, setHostAvailable] = useState(true);
  const [hostNote, setHostNote] = useState<HostNote | null>(null);
  const kind = useKindLabel(d);

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
  const hasHostFile = !!d.path && !isUrl;

  // A success note fades after a few seconds; "not possible here" stays.
  useEffect(() => {
    if (hostNote !== "opened" && hostNote !== "shown") return;
    const timer = window.setTimeout(() => setHostNote(null), 4000);
    return () => window.clearTimeout(timer);
  }, [hostNote]);

  async function onHost(reveal: boolean, subpath?: string) {
    try {
      await api.tasks.deliverables.open(boardId, taskId, d.id, reveal ? { reveal: true, subpath } : { reveal: false });
      setHostNote(reveal ? "shown" : "opened");
    } catch {
      setHostAvailable(false);
      setHostNote("unavailable");
    }
  }

  const toggle = () => canExpand && setExpanded((v) => !v);

  return (
    <motion.div
      initial={{ opacity: 0, x: -4 }}
      animate={{ opacity: 1, x: 0 }}
      transition={{ duration: 0.18, ease: "easeOut" }}
      style={{
        background: expanded ? C.accentSubtle : "var(--color-bg-surface)",
        border: expanded ? `1px solid ${C.borderAccent}` : `1px solid ${C.border}`,
        borderRadius: 12,
        overflow: "hidden",
        transition: "background 0.15s, border-color 0.15s",
      }}
    >
      {/* Card Header — the whole header opens and closes the card; nothing
          in it acts on the Mac. */}
      <div
        className={`flex items-start gap-2 px-3 py-2 ${canExpand ? "cursor-pointer" : ""}`}
        onClick={toggle}
        role={canExpand ? "button" : undefined}
        tabIndex={canExpand ? 0 : undefined}
        aria-expanded={canExpand ? expanded : undefined}
        onKeyDown={(e) => canExpand && e.key === "Enter" && toggle()}
      >
        <span className="mt-1 shrink-0" style={{ color: C.textMuted }}>
          <DeliverableIcon type={d.deliverable_type} />
        </span>

        <div className="flex-1 min-w-0">
          {/* Title + kind */}
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <span className="text-sm font-medium min-w-0" style={{ color: C.textPrimary }}>{d.title}</span>
            <span
              data-testid="deliverable-kind"
              className="text-xs px-2 rounded-sm shrink-0"
              style={{ background: C.bgElevated, color: C.textMuted }}
            >
              {kind}
            </span>
            {/* Source-Badge: nur wenn Deliverable aus Subtask stammt (source_depth > 0).
                Zeigt dem Reviewer woher der Output kommt ohne extra Navigation. */}
            {(d.source_depth ?? 0) > 0 && d.source_task_title && (
              <span
                className="text-xs px-2 rounded-sm shrink-0 truncate"
                title={t("fromSubtask", { title: d.source_task_title })}
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
            <p className="text-xs mt-1 truncate" style={{ color: C.textMuted }}>{d.description}</p>
          )}

          {/* Path — URL */}
          {d.path && isUrl && (
            <a
              href={d.path}
              target="_blank"
              rel="noopener noreferrer"
              className="flex items-center gap-1 mt-1 text-xs hover:underline cursor-pointer min-w-0"
              style={{ color: C.accent }}
              onClick={(e) => e.stopPropagation()}
            >
              <ExternalLink size={12} className="shrink-0" />
              <span className="break-all">{d.path}</span>
            </a>
          )}

          {/* Path — file path: a machine value, wrapped, never cut (K3). It
              only shows; tapping it opens the card like the rest of the
              header (it used to reveal the file in Finder on the Mac). */}
          {d.path && !isUrl && (
            <p className="mt-1 text-xs font-mono break-all" style={{ color: C.textMuted }}>{d.path}</p>
          )}

          {/* Agent + Date */}
          <div className="flex items-center gap-2 mt-1 text-xs" style={{ color: C.textMuted }}>
            {d.agent_name && <span>{d.agent_name}</span>}
            <span>{new Date(d.created_at).toLocaleString("de-CH", { dateStyle: "short", timeStyle: "short" })}</span>
          </div>
        </div>

        {canExpand && (
          <span className="shrink-0 mt-1" style={{ color: C.textMuted }} aria-hidden="true">
            {expanded ? <ChevronUp size={16} /> : <ChevronDown size={16} />}
          </span>
        )}
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
                {/* The Mac actions: desktop with a mouse only, in words, with
                    what happened next to them. */}
                {hasHostFile && (hostAvailable || hostNote) && (
                  <div
                    data-testid="deliverable-host-actions"
                    className={`${HOST_ONLY_CLASS} flex-wrap items-center gap-x-4 gap-y-1 mb-2`}
                  >
                    {hostAvailable && (
                      <>
                        <HostButton icon={<FolderOpen size={14} aria-hidden="true" />} onClick={() => onHost(false)}>
                          {t("openOnMac")}
                        </HostButton>
                        <HostButton icon={<FolderSearch size={14} aria-hidden="true" />} onClick={() => onHost(true)}>
                          {t("showInFinder")}
                        </HostButton>
                      </>
                    )}
                    <span role="status" className="text-xs" style={{ color: C.textMuted }}>
                      {hostNote ? t(HOST_NOTE_KEY[hostNote]) : null}
                    </span>
                  </div>
                )}
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
                    onOpenInFinder={hostAvailable ? (subpath) => onHost(true, subpath) : undefined}
                  />
                ) : fileUrl && d.path ? (
                  <FilePreview
                    fileUrl={fileUrl}
                    path={d.path}
                    htmlFolderUrl={() => api.tasks.deliverables.viewUrl(boardId, taskId, d.id)}
                  />
                ) : null}
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  );
}

/** A quiet text button (K11 "Neben") for a Mac action. */
function HostButton({ icon, onClick, children }: { icon: React.ReactNode; onClick: () => void; children: React.ReactNode }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="inline-flex items-center gap-2 min-h-8 text-xs cursor-pointer transition-colors"
      style={{ color: C.textSecondary }}
      onMouseEnter={(e) => (e.currentTarget.style.color = C.textPrimary)}
      onMouseLeave={(e) => (e.currentTarget.style.color = C.textSecondary)}
    >
      {icon}
      {children}
    </button>
  );
}
