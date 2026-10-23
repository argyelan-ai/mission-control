"use client";

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { Folder, FileText, ChevronRight, Loader2, AlertCircle } from "lucide-react";
import { api } from "@/lib/api";
import { FilePreview } from "./FilePreview";
import { C } from "@/lib/colors";
import { HOST_ONLY_CLASS } from "@/lib/hostOnly";

interface DirectoryBrowserProps {
  boardId: string;
  taskId: string;
  deliverableId: string;
  /** Optional — only provided while revealing in Finder may work here:
   *  the card passes it only for a viewer at the Mac MC runs on (MC opened
   *  via localhost) and drops it once the backend says no. The links also
   *  need a mouse (HOST_ONLY_CLASS): Finder opens on that Mac, never on the
   *  phone or laptop in hand. */
  onOpenInFinder?: (subpath?: string) => void;
  /** Called when the folder cannot be listed (gone, 404) — the deliverable
   *  card then drops its Mac buttons too (round-2 user test t3 N2). */
  onLoadError?: () => void;
}

export function DirectoryBrowser({
  boardId,
  taskId,
  deliverableId,
  onOpenInFinder,
  onLoadError,
}: DirectoryBrowserProps) {
  const t = useTranslations("deliverableCard");
  const [currentSubpath, setCurrentSubpath] = useState("");
  const [selectedFile, setSelectedFile] = useState<string | null>(null); // subpath of selected file

  const { data, isLoading, isError } = useQuery({
    queryKey: ["deliverable-dir", deliverableId, currentSubpath],
    queryFn: () => api.tasks.deliverables.directory(boardId, taskId, deliverableId, currentSubpath || undefined),
  });

  // Only the folder itself failing counts — not a sub-folder the reader
  // opened later.
  const rootFailed = isError && currentSubpath === "";
  const onLoadErrorRef = useRef(onLoadError);
  useLayoutEffect(() => {
    onLoadErrorRef.current = onLoadError;
  });
  useEffect(() => {
    if (rootFailed) onLoadErrorRef.current?.();
  }, [rootFailed]);

  // Breadcrumb parts from currentSubpath
  const parts = currentSubpath ? currentSubpath.split("/") : [];

  function navigateTo(subpath: string) {
    setCurrentSubpath(subpath);
    setSelectedFile(null);
  }

  function navigateToBreadcrumb(index: number) {
    // index -1 = root, 0 = parts[0], etc.
    if (index < 0) {
      navigateTo("");
    } else {
      navigateTo(parts.slice(0, index + 1).join("/"));
    }
  }

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-6">
        <Loader2 size={14} className="animate-spin" style={{ color: C.textMuted }} />
      </div>
    );
  }

  if (isError || !data) {
    return (
      <div className="flex items-center gap-2 py-4 px-2">
        <AlertCircle size={14} style={{ color: C.error }} />
        <span className="text-xs" style={{ color: C.textMuted }}>{t("folderFailed")}</span>
      </div>
    );
  }

  const selectedFileUrl = selectedFile
    ? `/api/v1/boards/${boardId}/tasks/${taskId}/deliverables/${deliverableId}/file?subpath=${encodeURIComponent(selectedFile)}`
    : null;

  const selectedFilePath = selectedFile ? selectedFile.split("/").pop() ?? "" : "";

  return (
    <div>
      {/* Breadcrumb */}
      <div className="flex items-center gap-1 mb-2 flex-wrap" style={{ fontSize: 11, color: C.textMuted }}>
        <button
          onClick={() => navigateToBreadcrumb(-1)}
          className="hover:underline cursor-pointer"
          style={{ color: parts.length > 0 ? C.accent : C.textMuted }}
        >
          {data.root_path.split("/").pop() || t("folderRoot")}
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

      {/* File list */}
      {data.entries.length === 0 ? (
        <p className="text-xs py-2" style={{ color: C.textMuted }}>{t("folderEmpty")}</p>
      ) : (
        <div className="flex flex-col gap-0.5">
          {data.entries.map((entry) => {
            const entrySubpath = currentSubpath ? `${currentSubpath}/${entry.name}` : entry.name;
            const isSelected = selectedFile === entrySubpath;

            return (
              <button
                key={entry.name}
                onClick={() => {
                  if (entry.type === "directory") {
                    navigateTo(entrySubpath);
                  } else {
                    setSelectedFile(isSelected ? null : entrySubpath);
                  }
                }}
                className="flex items-center gap-2 px-2 py-1.5 rounded-md text-left w-full transition-colors"
                style={{
                  background: isSelected ? C.accentSubtle : "transparent",
                  border: isSelected ? `1px solid ${C.borderAccent}` : "1px solid transparent",
                }}
                onMouseOver={(e) => {
                  if (!isSelected) (e.currentTarget as HTMLElement).style.background = "var(--color-bg-surface)";
                }}
                onMouseOut={(e) => {
                  if (!isSelected) (e.currentTarget as HTMLElement).style.background = "transparent";
                }}
              >
                {entry.type === "directory" ? (
                  <Folder size={12} style={{ color: C.accent, flexShrink: 0 }} />
                ) : (
                  <FileText size={12} style={{ color: C.textMuted, flexShrink: 0 }} />
                )}
                <span className="flex-1 text-xs truncate" style={{ color: C.textPrimary }}>
                  {entry.name}
                </span>
                {entry.type === "file" && entry.size !== null && (
                  <span className="text-xs shrink-0 font-mono" style={{ color: C.textMuted }}>
                    {entry.size < 1024
                      ? `${entry.size} B`
                      : entry.size < 1024 * 1024
                      ? `${(entry.size / 1024).toFixed(1)} KB`
                      : `${(entry.size / 1024 / 1024).toFixed(1)} MB`}
                  </span>
                )}
                {entry.type === "directory" && (
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
          <div className="flex items-center justify-between gap-2 mb-2">
            <span className="text-xs font-mono break-all" style={{ color: C.textMuted }}>
              {selectedFilePath}
            </span>
            {onOpenInFinder && (
              <button
                type="button"
                onClick={() => onOpenInFinder(selectedFile)}
                className={`${HOST_ONLY_CLASS} shrink-0 text-xs cursor-pointer hover:underline`}
                style={{ color: C.textSecondary }}
              >
                {t("showFileInFinder")}
              </button>
            )}
          </div>
          <FilePreview
            fileUrl={selectedFileUrl}
            path={selectedFilePath}
            htmlFolderUrl={() => api.tasks.deliverables.viewUrl(boardId, taskId, deliverableId, selectedFile)}
          />
        </div>
      )}

      {/* Footer: open root in Finder — only when revealing is available here */}
      {onOpenInFinder && (
        <div className="mt-2 pt-2" style={{ borderTop: `1px solid ${C.borderSubtle}` }}>
          <button
            type="button"
            onClick={() => onOpenInFinder()}
            className={`${HOST_ONLY_CLASS} text-xs cursor-pointer hover:underline`}
            style={{ color: C.textSecondary }}
          >
            {t("showFolderInFinder")}
          </button>
        </div>
      )}
    </div>
  );
}
