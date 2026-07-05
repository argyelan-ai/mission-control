"use client";

import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Folder, FileText, ChevronRight, Loader2, AlertCircle, FolderX, Download } from "lucide-react";
import { api, getToken } from "@/lib/api";
import { FilePreview } from "./FilePreview";
import { C } from "@/lib/colors";
import type { Task } from "@/lib/types";

interface WorkspaceTabProps {
  task: Task;
  boardId: string;
}

// Files above this size aren't auto-previewed — FilePreview loads the whole
// file into browser memory (blob or text), which can OOM the tab for a huge
// log/dump. Gate with an explicit "Load preview" step instead.
const AUTO_PREVIEW_MAX_BYTES = 5 * 1024 * 1024;

// Content endpoints require a Bearer token — a bare <a href> download would
// 401. Mirrors FilePreview's own DownloadButton (not exported from there).
async function downloadWorkspaceFile(fileUrl: string, fileName: string) {
  try {
    const res = await fetch(fileUrl, { headers: { Authorization: `Bearer ${getToken()}` } });
    if (!res.ok) throw new Error(`${res.status}`);
    const blob = await res.blob();
    const objectUrl = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = objectUrl;
    a.download = fileName;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(objectUrl);
  } catch {
    // silent — best-effort, same convention as FilePreview's download button
  }
}

export function WorkspaceTab({ task, boardId }: WorkspaceTabProps) {
  const [currentSubpath, setCurrentSubpath] = useState("");
  const [selectedFile, setSelectedFile] = useState<string | null>(null); // subpath of selected file
  const [selectedFileSize, setSelectedFileSize] = useState<number>(0);
  const [forcePreview, setForcePreview] = useState(false); // user opted into a large-file preview

  // Switching tasks reuses this component instance (no remount guaranteed —
  // the /tasks split view renders TaskDetailBody without a key={task.id}) —
  // reset navigation/selection so Task B doesn't inherit Task A's subpath.
  useEffect(() => {
    setCurrentSubpath("");
    setSelectedFile(null);
    setSelectedFileSize(0);
    setForcePreview(false);
  }, [task.id]);

  const { data, isLoading, isError } = useQuery({
    queryKey: ["task-workspace", task.id, currentSubpath],
    queryFn: () => api.tasks.workspace.list(boardId, task.id, currentSubpath || undefined),
  });

  // Breadcrumb parts from currentSubpath
  const parts = currentSubpath ? currentSubpath.split("/") : [];

  function navigateTo(subpath: string) {
    setCurrentSubpath(subpath);
    setSelectedFile(null);
    setSelectedFileSize(0);
    setForcePreview(false);
  }

  function navigateToBreadcrumb(index: number) {
    // index -1 = root, 0 = parts[0], etc.
    if (index < 0) {
      navigateTo("");
    } else {
      navigateTo(parts.slice(0, index + 1).join("/"));
    }
  }

  // Rendered both in the normal listing and in the error state below — a
  // subfolder that 404s mid-navigation (deleted while browsing) must still
  // let the user climb back out instead of dead-ending on a bare error.
  const breadcrumb = (
    <div className="flex items-center gap-1 mb-2 flex-wrap" style={{ fontSize: 11, color: C.textMuted }}>
      <button
        onClick={() => navigateToBreadcrumb(-1)}
        className="hover:underline cursor-pointer"
        style={{ color: parts.length > 0 ? C.accent : C.textMuted }}
      >
        workspace
      </button>
      {parts.map((part, i) => (
        <span key={i} className="flex items-center gap-1">
          <ChevronRight size={10} />
          <button
            onClick={() => navigateToBreadcrumb(i)}
            className="hover:underline cursor-pointer"
            style={{ color: i === parts.length - 1 ? C.textMuted : C.accent }}
          >
            {part}
          </button>
        </span>
      ))}
    </div>
  );

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-6">
        <Loader2 size={14} className="animate-spin" style={{ color: C.textMuted }} />
      </div>
    );
  }

  if (isError || !data) {
    return (
      <div>
        {parts.length > 0 && breadcrumb}
        <div className="flex items-center gap-2 py-4 px-2">
          <AlertCircle size={14} style={{ color: C.error }} />
          <span className="text-xs" style={{ color: C.textMuted }}>Failed to load workspace</span>
        </div>
      </div>
    );
  }

  if (!data.exists) {
    return (
      <div className="flex flex-col items-center justify-center py-8 gap-2 text-center">
        <FolderX size={18} style={{ color: C.textMuted }} />
        <p className="text-xs max-w-xs" style={{ color: C.textMuted }}>
          The workspace folder no longer exists. Any produced results live under the{" "}
          <span style={{ color: C.textSecondary }}>Deliverables</span> tab instead.
        </p>
      </div>
    );
  }

  const selectedFileUrl = selectedFile
    ? api.tasks.workspace.contentUrl(boardId, task.id, selectedFile)
    : null;

  const selectedFilePath = selectedFile ? selectedFile.split("/").pop() ?? "" : "";
  const selectedFileTooBig = selectedFileSize > AUTO_PREVIEW_MAX_BYTES;

  return (
    <div>
      {breadcrumb}

      {/* File list */}
      {data.entries.length === 0 ? (
        <p className="text-xs py-2" style={{ color: C.textMuted }}>Directory is empty</p>
      ) : (
        <div className="flex flex-col gap-0.5">
          {data.entries.map((entry) => {
            const entrySubpath = currentSubpath ? `${currentSubpath}/${entry.name}` : entry.name;
            const isSelected = selectedFile === entrySubpath;

            return (
              <button
                key={entry.name}
                onClick={() => {
                  if (entry.is_directory) {
                    navigateTo(entrySubpath);
                  } else {
                    setSelectedFile(isSelected ? null : entrySubpath);
                    setSelectedFileSize(isSelected ? 0 : entry.size);
                    setForcePreview(false);
                  }
                }}
                className="flex items-center gap-2 px-2 py-1.5 rounded-md text-left w-full transition-colors"
                style={{
                  background: isSelected ? C.accentSubtle : "transparent",
                  border: isSelected ? `1px solid ${C.borderAccent}` : "1px solid transparent",
                }}
                onMouseOver={(e) => {
                  if (!isSelected) (e.currentTarget as HTMLElement).style.background = "rgba(255,255,255,0.03)";
                }}
                onMouseOut={(e) => {
                  if (!isSelected) (e.currentTarget as HTMLElement).style.background = "transparent";
                }}
              >
                {entry.is_directory ? (
                  <Folder size={12} style={{ color: C.accent, flexShrink: 0 }} />
                ) : (
                  <FileText size={12} style={{ color: C.textMuted, flexShrink: 0 }} />
                )}
                <span className="flex-1 text-xs truncate" style={{ color: C.textPrimary }}>
                  {entry.name}
                </span>
                {!entry.is_directory && (
                  <span className="text-[10px] shrink-0 font-mono" style={{ color: C.textMuted }}>
                    {entry.size < 1024
                      ? `${entry.size} B`
                      : entry.size < 1024 * 1024
                      ? `${(entry.size / 1024).toFixed(1)} KB`
                      : `${(entry.size / 1024 / 1024).toFixed(1)} MB`}
                  </span>
                )}
                {entry.is_directory && (
                  <ChevronRight size={10} style={{ color: C.textMuted, flexShrink: 0 }} />
                )}
              </button>
            );
          })}
        </div>
      )}

      {/* Inline file preview when a file is selected */}
      {selectedFile && selectedFileUrl && (
        <div className="mt-3 pt-3" style={{ borderTop: `1px solid ${C.borderSubtle}` }}>
          <div className="flex items-center justify-between mb-2">
            <span className="text-[10px] font-mono" style={{ color: C.textMuted }}>
              {selectedFilePath}
            </span>
          </div>
          {selectedFileTooBig && !forcePreview ? (
            <div className="flex flex-col items-center justify-center gap-2 py-6 text-center">
              <p className="text-xs max-w-xs" style={{ color: C.textMuted }}>
                This file is {(selectedFileSize / 1024 / 1024).toFixed(1)} MB — too large to preview
                automatically.
              </p>
              <div className="flex items-center gap-2">
                <button
                  onClick={() => setForcePreview(true)}
                  className="px-2.5 py-1.5 rounded-md text-xs font-medium cursor-pointer transition-colors"
                  style={{ background: C.accentSubtle, color: C.accent, border: `1px solid ${C.borderAccent}` }}
                >
                  Load preview anyway
                </button>
                <button
                  onClick={() => downloadWorkspaceFile(selectedFileUrl, selectedFilePath)}
                  className="inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-md text-xs font-medium cursor-pointer transition-colors"
                  style={{ background: C.bgElevated, color: C.textSecondary, border: `1px solid ${C.border}` }}
                >
                  <Download size={12} />
                  Download
                </button>
              </div>
            </div>
          ) : (
            <FilePreview fileUrl={selectedFileUrl} path={selectedFilePath} />
          )}
        </div>
      )}
    </div>
  );
}
