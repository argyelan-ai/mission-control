"use client";

/**
 * GitPanel — branch / commits / inline diff section of the task detail.
 *
 * Extracted from TaskDetailPanel (07/2026 redesign). Reads live git state
 * from the task workspace via /git-info + /git-diff; renders the actual
 * diff through the shared <GitDiffView>. Active states use surface tint
 * instead of side-stripe borders (Flach-Regel).
 */

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { motion, AnimatePresence } from "framer-motion";
import { ChevronDown, ExternalLink, GitBranch, GitCommit } from "lucide-react";
import { api } from "@/lib/api";
import { C } from "@/lib/colors";
import { GitDiffView } from "@/components/git/GitDiffView";
import type { CommitDiff, TaskGitInfo } from "@/lib/types";

export function GitPanel({
  gitInfo,
  boardId,
  taskId,
}: {
  gitInfo: TaskGitInfo;
  boardId: string;
  taskId: string;
}) {
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

  const branchUrl = repoUrl && gitInfo.branch ? `${repoUrl}/tree/${gitInfo.branch}` : null;
  const commitUrl = (hash: string) => (repoUrl ? `${repoUrl}/commit/${hash}` : null);

  return (
    <div>
      {/* Summary row */}
      <div className="flex items-center gap-2 text-xs flex-wrap" style={{ color: C.textSecondary }}>
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
            <span className="font-medium truncate" style={{ color: C.textPrimary, maxWidth: 160 }}>
              {gitInfo.branch}
            </span>
          )}
        </span>

        {gitInfo.ahead > 0 && (
          <span className="flex items-center gap-1 shrink-0" style={{ color: C.textMuted }}>
            <GitCommit size={10} />
            <span className="text-[10px]">{gitInfo.ahead} ahead</span>
          </span>
        )}

        {gitInfo.uncommitted && (
          <span
            className="px-1.5 py-0.5 rounded text-[10px] font-medium shrink-0"
            style={{ background: `${C.warning}1A`, color: C.warning, border: `1px solid ${C.warning}33` }}
          >
            uncommitted
          </span>
        )}

        <span className="flex items-center gap-1.5 ml-auto shrink-0">
          {repoUrl && gitInfo.repo_name && (
            <a
              href={repoUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-medium hover:opacity-80 transition-opacity font-mono"
              style={{ background: C.bgElevated, color: C.textMuted, border: `1px solid ${C.border}` }}
              title={repoUrl}
            >
              {gitInfo.repo_name}
            </a>
          )}
          {gitInfo.pr_url && (
            <a
              href={gitInfo.pr_url}
              target="_blank"
              rel="noopener noreferrer"
              className="flex items-center gap-1 px-2 py-0.5 rounded text-[10px] font-medium cursor-pointer hover:opacity-80 transition-opacity"
              style={{ background: C.accentSubtle, color: C.accent, border: `1px solid ${C.borderAccent}` }}
            >
              <ExternalLink size={9} />
              PR open
            </a>
          )}
          {hasCommits && (
            <button
              onClick={() => setExpanded((x) => !x)}
              className="flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] transition-colors cursor-pointer"
              style={{
                background: expanded ? C.accentSubtle : "transparent",
                color: expanded ? C.accent : C.textDim,
                border: `1px solid ${expanded ? C.borderAccent : C.border}`,
              }}
              aria-expanded={expanded}
              aria-label={`${gitInfo.commits!.length} commits`}
            >
              <GitCommit size={10} />
              <span>{gitInfo.commits!.length}</span>
              <motion.span animate={{ rotate: expanded ? 180 : 0 }} transition={{ duration: 0.15 }} style={{ display: "block" }}>
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
            <div className="mt-2 pt-1" style={{ borderTop: `1px solid ${C.borderSubtle}` }}>
              {gitInfo.commits!.map((commit, i) => {
                const isActive = activeHash === commit.hash;
                const isLoading = diffLoading && isActive;
                const ghUrl = commitUrl(commit.hash);
                return (
                  <div key={commit.hash}>
                    <button
                      onClick={() => setActiveHash((prev) => (prev === commit.hash ? null : commit.hash))}
                      className="w-full flex items-start gap-3 px-1 py-2 text-left rounded-lg transition-colors cursor-pointer"
                      style={{ background: isActive ? C.accentSubtle : "transparent" }}
                      onMouseEnter={(e) => {
                        if (!isActive) (e.currentTarget as HTMLElement).style.background = "rgba(255,255,255,0.02)";
                      }}
                      onMouseLeave={(e) => {
                        if (!isActive) (e.currentTarget as HTMLElement).style.background = "transparent";
                      }}
                    >
                      <div className="flex flex-col items-center shrink-0 mt-1.5">
                        <div
                          className="w-1.5 h-1.5 rounded-full shrink-0"
                          style={{ background: isActive || i === 0 ? C.accent : C.bgHover }}
                        />
                        {i < gitInfo.commits!.length - 1 && (
                          <div className="w-px flex-1 mt-1" style={{ background: C.borderSubtle, minHeight: 12 }} />
                        )}
                      </div>
                      <div className="flex-1 min-w-0 pb-0.5">
                        <div className="flex items-baseline gap-2">
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
                            <span className="text-[10px] font-mono shrink-0" style={{ color: i === 0 ? C.accent : C.textDim }}>
                              {commit.hash}
                            </span>
                          )}
                          <span className="text-xs truncate" style={{ color: i === 0 ? C.textPrimary : C.textMuted }}>
                            {commit.message}
                          </span>
                        </div>
                        <div className="flex items-center gap-1.5 mt-0.5 text-[10px]" style={{ color: C.textDim }}>
                          <span>{commit.author}</span>
                          <span>·</span>
                          <span>{commit.date}</span>
                        </div>
                      </div>
                      <div className="shrink-0 mt-1.5">
                        {isLoading ? (
                          <motion.div
                            animate={{ rotate: 360 }}
                            transition={{ repeat: Infinity, duration: 0.8, ease: "linear" }}
                            style={{
                              width: 10,
                              height: 10,
                              borderRadius: "50%",
                              border: `1.5px solid ${C.accentSubtle}`,
                              borderTopColor: C.accent,
                            }}
                          />
                        ) : (
                          <motion.span
                            animate={{ rotate: isActive ? 180 : 0 }}
                            transition={{ duration: 0.15 }}
                            style={{ color: isActive ? C.accent : C.textDim, display: "block" }}
                          >
                            <ChevronDown size={10} />
                          </motion.span>
                        )}
                      </div>
                    </button>

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
