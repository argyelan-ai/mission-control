"use client";

/**
 * ChangesPanel — "what did the agent build": the task branch diffed against
 * its base (three-dot), as a file list with +/− and expandable hunks. Reuses
 * <FileDiff> from the shared git diff view; a per-commit view stays available
 * in the Technical tab's Git section.
 */

import { useState } from "react";
import { useTranslations } from "next-intl";
import { useQuery } from "@tanstack/react-query";
import { GitBranch, ExternalLink } from "lucide-react";
import { api } from "@/lib/api";
import { C } from "@/lib/colors";
import { FileDiff } from "@/components/git/GitDiffView";
import type { BranchDiff, Task, TaskGitInfo } from "@/lib/types";

const INITIAL_FILES = 8;

export function useBranchDiff(task: Task, boardId: string) {
  return useQuery<BranchDiff>({
    queryKey: ["task-branch-diff", boardId, task.id],
    queryFn: () => api.tasks.gitBranchDiff(boardId, task.id),
    enabled: !!task.workspace_path,
    staleTime: 30_000,
    refetchInterval: task.status === "in_progress" ? 60_000 : false,
  });
}

export function ChangesPanel({
  task,
  boardId,
  gitInfo,
}: {
  task: Task;
  boardId: string;
  gitInfo?: TaskGitInfo | null;
}) {
  const t = useTranslations("tasks");
  const [showAll, setShowAll] = useState(false);
  const { data: diff, isLoading, isError } = useBranchDiff(task, boardId);

  if (!task.workspace_path) {
    return <Empty>{t("changesNoWorkspace")}</Empty>;
  }
  if (isError) return <Empty tone="error">{t("changesLoadFailed")}</Empty>;
  if (isLoading || !diff) return <Skeleton />;
  if (!Array.isArray(diff.files) || diff.files.length === 0) return <Empty>{t("changesEmpty")}</Empty>;

  const files = showAll ? diff.files : diff.files.slice(0, INITIAL_FILES);
  const hidden = diff.files.length - files.length;
  const prUrl = gitInfo?.pr_url ?? null;

  return (
    <div>
      {/* Header: branch · totals */}
      <div className="flex items-center gap-2 flex-wrap text-[11px] mb-2" style={{ color: C.textMuted }}>
        <span className="inline-flex items-center gap-1 min-w-0">
          <GitBranch size={11} style={{ color: C.accent }} />
          <span className="truncate font-mono" style={{ color: C.textSecondary }}>{gitInfo?.branch ?? task.branch_name ?? "—"}</span>
        </span>
        <span style={{ color: C.textDim }}>{t("changesVsBase", { base: diff.base })}</span>
        <span className="ml-auto flex items-center gap-2 font-mono shrink-0">
          <span>{t("changesCommits", { count: diff.commits })}</span>
          <span>{t("changesFiles", { count: diff.stats.files })}</span>
          <span style={{ color: C.online, fontWeight: 600 }}>+{diff.stats.additions}</span>
          <span style={{ color: C.error, fontWeight: 600 }}>−{diff.stats.deletions}</span>
        </span>
        {prUrl && (
          <a
            href={prUrl}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1 rounded-md px-1.5 py-0.5"
            style={{ color: C.textSecondary, border: `1px solid ${C.border}` }}
          >
            PR <ExternalLink size={10} />
          </a>
        )}
      </div>

      {/* Files */}
      <div className="rounded-lg overflow-hidden" style={{ border: `1px solid ${C.border}` }}>
        {files.map((file) => (
          <FileDiff key={file.filename} file={file} defaultOpen={false} />
        ))}
      </div>
      {hidden > 0 && (
        <button
          type="button"
          onClick={() => setShowAll(true)}
          className="mt-2 text-[11px] cursor-pointer"
          style={{ color: C.textMuted }}
        >
          {t("changesShowAll")} · {t("changesMore", { count: hidden })}
        </button>
      )}
    </div>
  );
}

function Empty({ children, tone }: { children: React.ReactNode; tone?: "error" }) {
  return (
    <div className="text-xs py-3" style={{ color: tone === "error" ? C.error : C.textMuted }}>
      {children}
    </div>
  );
}

function Skeleton() {
  return (
    <div className="space-y-1.5 py-1" aria-busy="true">
      {[0, 1, 2].map((i) => (
        <div key={i} className="h-7 rounded-md" style={{ background: C.bgSurface, opacity: 1 - i * 0.25 }} />
      ))}
    </div>
  );
}
