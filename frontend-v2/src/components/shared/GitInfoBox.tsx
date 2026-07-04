"use client";

import { useState } from "react";
import { GitBranch, ExternalLink, Pencil, Check, AlertTriangle, Loader2 } from "lucide-react";
import type { ProjectGitInfo } from "@/lib/types";

interface GitInfoBoxProps {
  gitInfo: ProjectGitInfo | null | undefined;
  isLoading: boolean;
  autoSlug: string;
  branchName: string;
  onBranchNameChange: (name: string) => void;
  onInitRepo: () => void;
  initLoading: boolean;
  adHocMode?: boolean;
  useSeparateRepo?: boolean;
  onUseSeparateRepoChange?: (val: boolean) => void;
  accent?: string;
  textPrimary?: string;
  textMuted?: string;
  textSecondary?: string;
  border?: string;
  deep?: string;
  warning?: string;
}

export function GitInfoBox({
  gitInfo,
  isLoading,
  autoSlug,
  branchName,
  onBranchNameChange,
  onInitRepo,
  initLoading,
  adHocMode = false,
  useSeparateRepo = false,
  onUseSeparateRepoChange,
  accent = "#0FA3A3",
  textPrimary = "#EDEDEF",
  textMuted = "#888888",
  textSecondary = "#8A8F98",
  border = "rgba(255,255,255,0.06)",
  deep = "#020203",
  warning = "#F59E0B",
}: GitInfoBoxProps) {
  const [editingBranch, setEditingBranch] = useState(false);

  if (isLoading) {
    return (
      <div
        className="flex items-center gap-2 px-3 py-2.5 rounded-xl text-[11px]"
        style={{ backgroundColor: `${deep}88`, border: `1px solid ${border}` }}
      >
        <Loader2 size={12} className="animate-spin" style={{ color: textMuted }} />
        <span style={{ color: textMuted }}>Git-Info laden...</span>
      </div>
    );
  }

  if (adHocMode) {
    return (
      <div
        className="flex items-center gap-3 px-3 py-2.5 rounded-xl text-[11px]"
        style={{ backgroundColor: `${deep}88`, border: `1px solid ${border}` }}
      >
        <label className="flex items-center gap-1.5 cursor-pointer">
          <input
            type="checkbox"
            checked={useSeparateRepo}
            onChange={(e) => onUseSeparateRepoChange?.(e.target.checked)}
            className="w-3 h-3"
            style={{ accentColor: accent }}
          />
          <GitBranch size={11} style={{ color: accent }} />
          <span style={{ color: textSecondary }}>Eigenes Repo</span>
        </label>
        <span className="w-px h-3" style={{ backgroundColor: border }} />
        <span style={{ color: textMuted }}>Branch:</span>
        {editingBranch ? (
          <div className="flex items-center gap-1 flex-1">
            <input
              autoFocus
              aria-label="Branch-Name"
              value={branchName}
              onChange={(e) => onBranchNameChange(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter" || e.key === "Escape") setEditingBranch(false); }}
              onBlur={() => setEditingBranch(false)}
              className="flex-1 bg-transparent text-[11px] outline-none px-1 py-0.5 rounded"
              style={{ color: textPrimary, border: `1px solid ${accent}44` }}
            />
            <Check size={10} style={{ color: accent }} />
          </div>
        ) : (
          <button
            type="button"
            onClick={() => setEditingBranch(true)}
            className="flex items-center gap-1 cursor-pointer hover:opacity-80"
          >
            <span style={{ color: textPrimary }}>{branchName || `task/${autoSlug}`}</span>
            <Pencil size={9} style={{ color: textMuted }} />
          </button>
        )}
      </div>
    );
  }

  if (gitInfo && !gitInfo.has_repo) {
    return (
      <div
        className="flex items-center gap-2 px-3 py-2.5 rounded-xl text-[11px]"
        style={{ backgroundColor: `${deep}88`, border: `1px solid ${warning}33` }}
      >
        <AlertTriangle size={12} style={{ color: warning }} />
        <span style={{ color: textSecondary }}>Projekt hat noch kein Repository</span>
        <button
          type="button"
          onClick={onInitRepo}
          disabled={initLoading}
          className="ml-auto px-2.5 py-1 rounded-lg text-[10px] font-medium cursor-pointer transition-all disabled:opacity-40"
          style={{ backgroundColor: `${accent}22`, color: accent, border: `1px solid ${accent}66` }}
        >
          {initLoading ? "..." : "Repo initialisieren"}
        </button>
      </div>
    );
  }

  if (gitInfo?.has_repo) {
    const maxBranches = 5;
    const visibleBranches = gitInfo.branches.slice(0, maxBranches);
    const remaining = gitInfo.branches.length - maxBranches;

    return (
      <div
        className="flex flex-col gap-2 px-3 py-2.5 rounded-xl text-[11px]"
        style={{ backgroundColor: `${deep}88`, border: `1px solid ${border}` }}
      >
        <div className="flex items-center gap-2">
          <GitBranch size={12} style={{ color: accent }} />
          <span style={{ color: textPrimary }}>{gitInfo.repo_name}</span>
          {gitInfo.repo_url && (
            <a
              href={gitInfo.repo_url}
              target="_blank"
              rel="noopener noreferrer"
              className="hover:opacity-80 transition-opacity"
            >
              <ExternalLink size={10} style={{ color: textMuted }} />
            </a>
          )}
        </div>

        {visibleBranches.length > 0 && (
          <div className="flex items-center gap-1 flex-wrap pl-5">
            {visibleBranches.map((b) => (
              <span
                key={b}
                className="px-1.5 py-0.5 rounded text-[9px]"
                style={{ backgroundColor: `${accent}11`, color: textSecondary, border: `1px solid ${border}` }}
              >
                {b}
              </span>
            ))}
            {remaining > 0 && (
              <span className="text-[9px]" style={{ color: textMuted }}>+{remaining} weitere</span>
            )}
          </div>
        )}

        <div className="flex items-center gap-1.5 pl-5">
          <span style={{ color: textMuted }}>Task-Branch:</span>
          {editingBranch ? (
            <div className="flex items-center gap-1 flex-1">
              <input
                autoFocus
                aria-label="Branch-Name"
                value={branchName}
                onChange={(e) => onBranchNameChange(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter" || e.key === "Escape") setEditingBranch(false); }}
                onBlur={() => setEditingBranch(false)}
                className="flex-1 bg-transparent text-[11px] outline-none px-1 py-0.5 rounded"
                style={{ color: textPrimary, border: `1px solid ${accent}44` }}
              />
            </div>
          ) : (
            <button
              type="button"
              onClick={() => setEditingBranch(true)}
              className="flex items-center gap-1 cursor-pointer hover:opacity-80"
            >
              <span style={{ color: textPrimary }}>{branchName || `task/${autoSlug}`}</span>
              <Pencil size={9} style={{ color: textMuted }} />
            </button>
          )}
        </div>
      </div>
    );
  }

  return null;
}
