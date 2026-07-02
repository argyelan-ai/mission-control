"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { FolderSearch, Loader2 } from "lucide-react";
import { api } from "@/lib/api";
import type { FsRoot } from "@/lib/types";
import { C } from "@/lib/colors";
import { timeAgo } from "@/lib/utils";
import { SlideOverPanel } from "@/components/shared/SlideOverPanel";
import { FilePreview } from "@/components/task/FilePreview";
import { fileIcon, fileIconColor, humanSize, mtimeToIso } from "./fileUtils";

interface FilePreviewPanelProps {
  root: FsRoot;
  /** Subpath of the file to preview, relative to the root. Null → panel closed. */
  subpath: string | null;
  onClose: () => void;
}

export function FilePreviewPanel({ root, subpath, onClose }: FilePreviewPanelProps) {
  const open = subpath !== null;

  const { data: meta } = useQuery({
    queryKey: ["files-meta", root.key, subpath],
    queryFn: () => api.files.meta(root.key, subpath!),
    enabled: open,
  });

  const name = subpath ? subpath.split("/").pop() ?? subpath : "";
  const fileUrl = subpath ? api.files.contentUrl(root.key, subpath) : "";

  const Icon = fileIcon(name, false);
  const iconColor = fileIconColor(name, false);

  return (
    <SlideOverPanel open={open} onClose={onClose} title={name} desktopWidth="640px">
      {open && (
        <div className="p-4">
          {/* Meta header */}
          <div className="flex items-start gap-3 mb-4">
            <Icon size={20} style={{ color: iconColor, flexShrink: 0, marginTop: 2 }} />
            <div className="min-w-0 flex-1">
              <div className="text-sm font-medium break-all" style={{ color: C.textPrimary }}>{name}</div>
              <div className="flex flex-wrap items-center gap-x-3 gap-y-1 mt-1 text-xs" style={{ color: C.textMuted }}>
                <span className="font-mono">{root.label}</span>
                {meta && <span className="tabular-nums">{humanSize(meta.size)}</span>}
                {meta && <span>{timeAgo(mtimeToIso(meta.mtime))}</span>}
                {meta?.agent_slug && <span>{meta.agent_slug}</span>}
              </div>
            </div>
            {/* Finder — macOS bonus only; hidden on mobile / when no host path. */}
            {meta?.native_open_available && (
              <FinderButton root={root} subpath={subpath!} />
            )}
          </div>

          {/* Preview (carries its own always-present Download control) */}
          <div
            className="rounded-xl p-3"
            style={{ background: C.bgDeep, border: `1px solid ${C.borderSubtle}` }}
          >
            <FilePreview fileUrl={fileUrl} path={subpath!} />
          </div>
        </div>
      )}
    </SlideOverPanel>
  );
}

function FinderButton({ root, subpath }: { root: FsRoot; subpath: string }) {
  const [busy, setBusy] = useState(false);
  const [hidden, setHidden] = useState(false);

  if (hidden) return null;

  async function revealInFinder() {
    if (busy) return;
    setBusy(true);
    try {
      await api.files.open(root.key, subpath, true);
    } catch {
      // 409 / 501 → not available here after all. Hide gracefully.
      setHidden(true);
    } finally {
      setBusy(false);
    }
  }

  return (
    <button
      onClick={revealInFinder}
      disabled={busy}
      className="inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-md text-xs font-medium shrink-0 transition-colors cursor-pointer disabled:opacity-60"
      style={{ background: C.accentSubtle, color: C.accent, border: `1px solid ${C.borderAccent}` }}
      title="Im Finder anzeigen"
    >
      {busy ? <Loader2 size={12} className="animate-spin" /> : <FolderSearch size={12} />}
      Im Finder
    </button>
  );
}
