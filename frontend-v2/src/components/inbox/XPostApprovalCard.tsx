"use client";

import { motion } from "framer-motion";
import { CheckCircle, Send, XCircle } from "lucide-react";
import { timeAgo } from "@/lib/utils";
import { GlassCard } from "@/components/shared/GlassCard";
import { FilePreview } from "@/components/task/FilePreview";
import { api } from "@/lib/api";
import { C } from "@/lib/colors";
import type { Approval, XPostApprovalPayload } from "@/lib/types";

// Mirrors backend services/x_publisher.MAX_TWEET_LENGTH — the backend counts
// plain len(text) (no t.co URL weighting), so the frontend counter matches it.
export const MAX_TWEET_LENGTH = 280;

// Same extension set FilePreview classifies as video.
const VIDEO_EXTS = new Set(["mp4", "mov", "webm", "mkv", "avi"]);

// Browsable Files-API roots media can live in (subset of backend
// services/fs_roots._ROOTS — sensitive roots like "secrets" are excluded
// on purpose; the backend would refuse them anyway).
const FILES_ROOT_KEYS = new Set([
  "deliverables",
  "workspaces",
  "vault",
  "attachments",
  "references",
  "mcp-screenshots",
  "media",
  "shared-artifacts",
  "storyboard-images",
]);

export interface FilesLocation {
  root: string;
  subpath: string;
}

/** Maps an absolute media path from the approval payload onto a browsable
 *  Files-API location: either the mc-playwright sidecar volume
 *  (`/shared-deliverables/...`) or a host `~/.mc/<root>/...` path.
 *  Returns null when the path lies outside every browsable root — the card
 *  then shows the raw path as text instead of a broken player. */
export function mediaPathToFilesLocation(path: string): FilesLocation | null {
  const sidecarPrefix = "/shared-deliverables/";
  if (path.startsWith(sidecarPrefix)) {
    return { root: "shared-deliverables", subpath: path.slice(sidecarPrefix.length) };
  }
  const marker = "/.mc/";
  const idx = path.indexOf(marker);
  if (idx >= 0) {
    const rel = path.slice(idx + marker.length); // e.g. "deliverables/bench-1/a.png"
    const slash = rel.indexOf("/");
    if (slash > 0) {
      const root = rel.slice(0, slash);
      const subpath = rel.slice(slash + 1);
      if (FILES_ROOT_KEYS.has(root) && subpath) return { root, subpath };
    }
  }
  return null;
}

function isVideoPath(path: string): boolean {
  const name = path.split("/").pop() ?? path;
  const dot = name.lastIndexOf(".");
  const ext = dot >= 0 ? name.slice(dot + 1).toLowerCase() : "";
  return VIDEO_EXTS.has(ext);
}

function MediaItem({ path, thumbnail }: { path: string; thumbnail?: boolean }) {
  const loc = mediaPathToFilesLocation(path);
  if (!loc) {
    return (
      <code className="block text-[11px] font-mono break-all text-[var(--color-text-muted)]">
        {path}
      </code>
    );
  }
  const preview = (
    <FilePreview
      fileUrl={api.files.contentUrl(loc.root, loc.subpath)}
      path={path}
      showDownload={false}
    />
  );
  if (!thumbnail) return preview;
  return (
    <div
      className="w-44 rounded-xl overflow-hidden"
      style={{ border: "1px solid rgba(255,255,255,0.06)" }}
    >
      {preview}
    </div>
  );
}

// ── Component ─────────────────────────────────────────────────────────────────

interface Props {
  approval: Approval;
  onResolve: (status: "approved" | "rejected", note?: string) => void;
  loading?: boolean;
}

export function XPostApprovalCard({ approval, onResolve, loading }: Props) {
  const payload = (approval.payload ?? {}) as unknown as XPostApprovalPayload;
  const text = payload.text ?? "";
  const over = text.length > MAX_TWEET_LENGTH;
  const mediaPaths = payload.media_paths ?? []; // v1 text-only drafts: absent
  const videos = mediaPaths.filter(isVideoPath);
  const images = mediaPaths.filter((p) => !isVideoPath(p));

  return (
    <motion.div
      layout
      initial={{ opacity: 0, x: -8 }}
      animate={{ opacity: 1, x: 0 }}
      exit={{ opacity: 0, x: 8, height: 0 }}
    >
      <GlassCard className="p-4" glow={`${C.accent}12`}>
        {/* Header */}
        <div className="flex items-center gap-2 flex-wrap">
          <span
            className="text-[11px] px-2 py-0.5 rounded-lg font-medium flex items-center gap-1.5"
            style={{
              backgroundColor: `${C.accent}18`,
              color: C.accent,
              border: `1px solid ${C.accent}30`,
            }}
          >
            <Send size={12} /> X Post
          </span>
          {payload.requester_agent_name && (
            <span className="text-[10px] text-[var(--color-text-muted)]">
              from {payload.requester_agent_name}
            </span>
          )}
          <span className="text-[10px] ml-auto text-[var(--color-text-muted)]">
            {timeAgo(approval.created_at)}
          </span>
        </div>

        {/* Tweet-style preview */}
        <div
          className="mt-3 rounded-xl px-3.5 py-3"
          style={{
            backgroundColor: "rgba(255,255,255,0.03)",
            border: "1px solid rgba(255,255,255,0.06)",
          }}
        >
          <p className="text-sm whitespace-pre-wrap leading-relaxed text-[var(--color-text-primary)]">
            {text}
          </p>
          <div className="flex justify-end mt-2">
            <span
              className="text-[10px] font-mono"
              data-over={over ? "true" : undefined}
              style={{ color: over ? C.error : "var(--color-text-muted)" }}
            >
              {text.length}/{MAX_TWEET_LENGTH}
            </span>
          </div>

          {/* Media: 1 video full width, images as thumbnail grid */}
          {mediaPaths.length > 0 && (
            <div className="mt-3 space-y-2">
              {videos.map((p) => (
                <MediaItem key={p} path={p} />
              ))}
              {images.length > 0 && (
                <div className="flex gap-2 flex-wrap">
                  {images.map((p) => (
                    <MediaItem key={p} path={p} thumbnail />
                  ))}
                </div>
              )}
            </div>
          )}
        </div>

        {/* Actions */}
        <div
          className="flex items-center gap-2 mt-3 pt-3 border-t"
          style={{ borderColor: "rgba(255,255,255,0.06)" }}
        >
          <button
            onClick={() => onResolve("approved")}
            disabled={loading}
            className="flex items-center gap-1.5 text-[12px] px-3.5 py-2 rounded-xl cursor-pointer transition-all disabled:opacity-50"
            style={{
              backgroundColor: `${C.online}1F`,
              color: C.online,
              border: `1px solid ${C.online}40`,
            }}
          >
            <CheckCircle size={13} /> Approve &amp; post
          </button>
          <button
            onClick={() => onResolve("rejected")}
            disabled={loading}
            className="flex items-center gap-1.5 text-[12px] px-3.5 py-2 rounded-xl cursor-pointer transition-all disabled:opacity-50"
            style={{
              backgroundColor: `${C.error}1F`,
              color: C.error,
              border: `1px solid ${C.error}40`,
            }}
          >
            <XCircle size={13} /> Reject
          </button>
        </div>
      </GlassCard>
    </motion.div>
  );
}
