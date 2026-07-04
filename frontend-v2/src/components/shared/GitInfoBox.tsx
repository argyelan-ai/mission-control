"use client";

import { useState } from "react";
import { GitBranch, ExternalLink, Pencil, Check, AlertTriangle, Loader2 } from "lucide-react";
import type { ProjectGitInfo, Repo } from "@/lib/types";

function hasRules(repo: Repo): boolean {
  return Boolean(repo.rules_md && repo.rules_md.trim());
}

interface GitInfoBoxProps {
  gitInfo: ProjectGitInfo | null | undefined;
  isLoading: boolean;
  autoSlug: string;
  branchName: string;
  onBranchNameChange: (name: string) => void;
  onInitRepo: () => void;
  initLoading: boolean;
  adHocMode?: boolean;
  // Repo Registry (ADR-052) — single source of repo selection, replaces the
  // old per-task "Eigenes Repo" toggle.
  repos?: Repo[];
  repoId?: string | null;
  onRepoIdChange?: (id: string | null) => void;
  onCreateRepo?: (name: string) => Promise<Repo>;
  onLinkRepo?: (repoId: string) => Promise<void>;
  accent?: string;
  textPrimary?: string;
  textMuted?: string;
  textSecondary?: string;
  border?: string;
  deep?: string;
  warning?: string;
  online?: string;
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
  repos = [],
  repoId = null,
  onRepoIdChange,
  onCreateRepo,
  onLinkRepo,
  accent = "#0FA3A3",
  textPrimary = "#EDEDEF",
  textMuted = "#888888",
  textSecondary = "#8A8F98",
  border = "rgba(255,255,255,0.06)",
  deep = "#020203",
  warning = "#F59E0B",
  online = "#2B9A4A",
}: GitInfoBoxProps) {
  const [editingBranch, setEditingBranch] = useState(false);

  // ── Ad-hoc repo select (create-inline) ──
  const [showCreateRepo, setShowCreateRepo] = useState(false);
  const [newRepoName, setNewRepoName] = useState("");
  const [creatingRepo, setCreatingRepo] = useState(false);

  const handleCreateRepo = async () => {
    const name = newRepoName.trim();
    if (!name || creatingRepo || !onCreateRepo) return;
    setCreatingRepo(true);
    try {
      const repo = await onCreateRepo(name);
      onRepoIdChange?.(repo.id);
      setShowCreateRepo(false);
      setNewRepoName("");
    } catch {
      // Parent already surfaced the error (notify) — keep the name so the user can retry.
    } finally {
      setCreatingRepo(false);
    }
  };

  // ── Link-existing-repo (project selected, no repo yet) ──
  const [showLinkRepo, setShowLinkRepo] = useState(false);
  const [linkRepoId, setLinkRepoId] = useState("");
  const [linkingRepo, setLinkingRepo] = useState(false);

  const handleLinkRepo = async () => {
    if (!linkRepoId || linkingRepo || !onLinkRepo) return;
    setLinkingRepo(true);
    try {
      await onLinkRepo(linkRepoId);
      setShowLinkRepo(false);
      setLinkRepoId("");
    } catch {
      // Parent already surfaced the error (notify) — keep the selection so the user can retry.
    } finally {
      setLinkingRepo(false);
    }
  };

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
        className="flex flex-col gap-2 px-3 py-2.5 rounded-xl text-[11px]"
        style={{ backgroundColor: `${deep}88`, border: `1px solid ${border}` }}
      >
        <div className="flex items-center gap-2">
          <GitBranch size={11} style={{ color: accent }} />
          {showCreateRepo ? (
            <div className="flex items-center gap-1.5 flex-1">
              <input
                autoFocus
                aria-label="New repository name"
                value={newRepoName}
                onChange={(e) => setNewRepoName(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") handleCreateRepo();
                  if (e.key === "Escape") { setShowCreateRepo(false); setNewRepoName(""); }
                }}
                placeholder="repo-name"
                className="flex-1 bg-transparent text-[11px] outline-none px-1.5 py-1 rounded"
                style={{ color: textPrimary, border: `1px solid ${accent}44` }}
              />
              <button
                type="button"
                onClick={handleCreateRepo}
                disabled={!newRepoName.trim() || creatingRepo}
                className="px-2 py-1 rounded-lg text-[10px] font-medium cursor-pointer disabled:opacity-40"
                style={{ backgroundColor: `${accent}22`, color: accent, border: `1px solid ${accent}66` }}
              >
                {creatingRepo ? "..." : "Create"}
              </button>
              <button
                type="button"
                onClick={() => { setShowCreateRepo(false); setNewRepoName(""); }}
                className="px-2 py-1 rounded-lg text-[10px] cursor-pointer"
                style={{ color: textMuted }}
              >
                Cancel
              </button>
            </div>
          ) : (
            <select
              aria-label="Repository"
              value={repoId ?? ""}
              onChange={(e) => {
                if (e.target.value === "__create__") { setShowCreateRepo(true); return; }
                onRepoIdChange?.(e.target.value || null);
              }}
              className="flex-1 bg-transparent text-[11px] outline-none px-1.5 py-1 rounded cursor-pointer"
              style={{
                color: repoId ? textPrimary : textSecondary,
                border: `1px solid ${repoId ? `${accent}44` : border}`,
              }}
            >
              <option value="">No repository (default)</option>
              {repos.map((r) => (
                <option key={r.id} value={r.id}>
                  {r.full_name}{hasRules(r) ? " · Rules ✓" : ""}
                </option>
              ))}
              <option value="__create__">+ Create new repository&hellip;</option>
            </select>
          )}
        </div>
        <div className="flex items-center gap-1.5">
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
      </div>
    );
  }

  if (gitInfo && !gitInfo.has_repo) {
    return (
      <div
        className="flex flex-col gap-2 px-3 py-2.5 rounded-xl text-[11px]"
        style={{ backgroundColor: `${deep}88`, border: `1px solid ${warning}33` }}
      >
        <div className="flex items-center gap-2">
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
        {repos.length > 0 && (
          <div className="flex items-center gap-1.5 pl-5">
            {showLinkRepo ? (
              <>
                <select
                  aria-label="Link existing repository"
                  value={linkRepoId}
                  onChange={(e) => setLinkRepoId(e.target.value)}
                  className="flex-1 bg-transparent text-[11px] outline-none px-1.5 py-1 rounded cursor-pointer"
                  style={{ color: linkRepoId ? textPrimary : textSecondary, border: `1px solid ${border}` }}
                >
                  <option value="">Select repository&hellip;</option>
                  {repos.map((r) => (
                    <option key={r.id} value={r.id}>{r.full_name}</option>
                  ))}
                </select>
                <button
                  type="button"
                  onClick={handleLinkRepo}
                  disabled={!linkRepoId || linkingRepo}
                  className="px-2 py-1 rounded-lg text-[10px] font-medium cursor-pointer disabled:opacity-40"
                  style={{ backgroundColor: `${accent}22`, color: accent, border: `1px solid ${accent}66` }}
                >
                  {linkingRepo ? "..." : "Link"}
                </button>
                <button
                  type="button"
                  onClick={() => { setShowLinkRepo(false); setLinkRepoId(""); }}
                  className="px-2 py-1 rounded-lg text-[10px] cursor-pointer"
                  style={{ color: textMuted }}
                >
                  Cancel
                </button>
              </>
            ) : (
              <button
                type="button"
                onClick={() => setShowLinkRepo(true)}
                className="text-[10px] cursor-pointer hover:underline"
                style={{ color: accent }}
              >
                Link existing repo
              </button>
            )}
          </div>
        )}
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
          <a
            href="/repos"
            className="ml-auto flex items-center gap-1 text-[10px] hover:opacity-80 transition-opacity"
            style={{ color: gitInfo.has_rules ? online : textMuted }}
          >
            {gitInfo.has_rules ? (
              <>
                <Check size={10} />
                Rules active
              </>
            ) : (
              "No repo rules yet"
            )}
          </a>
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
