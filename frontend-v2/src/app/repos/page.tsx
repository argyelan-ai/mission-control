"use client";

/**
 * Repos registry (ADR-050) — manage GitHub repos + per-repo working rules.
 *
 * A repo can be shared by multiple projects; rules_md is included in every
 * agent dispatch for that repo (dispatch_message_builder). Deleting only
 * removes the MC registry row — GitHub is untouched.
 */

import { useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import Link from "next/link";
import { motion } from "framer-motion";
import { useQuery } from "@tanstack/react-query";
import { FolderGit2, GitBranch, Github, Loader2, Lock, Globe2, Plus } from "lucide-react";
import AppShell from "@/components/layout/AppShell";
import { api } from "@/lib/api";
import type { GithubStatus, Repo } from "@/lib/types";
import { C } from "@/lib/colors";
import { timeAgo } from "@/lib/utils";
import { RepoDetailPanel } from "./RepoDetailPanel";
import { ImportRepoDialog } from "./ImportRepoDialog";

// ── Visibility Badge ──────────────────────────────────────────────────────────

function VisibilityBadge({ visibility }: { visibility: Repo["visibility"] }) {
  const t = useTranslations("repos");
  const isPrivate = visibility === "private";
  const Icon = isPrivate ? Lock : Globe2;
  return (
    <span
      className="inline-flex items-center gap-1 shrink-0 uppercase"
      style={{
        background: C.border,
        color: C.textMuted,
        fontSize: "9px",
        padding: "1px 5px",
        borderRadius: "4px",
        letterSpacing: "0.06em",
      }}
    >
      <Icon size={9} />
      {isPrivate ? t("private") : t("public")}
    </span>
  );
}

// ── Repo Card ──────────────────────────────────────────────────────────────────

function RepoCard({ repo, onClick }: { repo: Repo; onClick: () => void }) {
  const t = useTranslations("repos");
  const locale = useLocale();
  const projects = repo.linked_projects;
  const shownProjects = projects.slice(0, 3);
  const restCount = projects.length - shownProjects.length;

  return (
    <motion.button
      onClick={onClick}
      initial={{ opacity: 0, x: -4 }}
      animate={{ opacity: 1, x: 0 }}
      transition={{ duration: 0.18, ease: [0.16, 1, 0.3, 1] }}
      className="flex flex-col gap-2 px-3 py-2.5 text-left cursor-pointer transition-colors w-full"
      style={{
        background: C.borderSubtle,
        border: `1px solid ${C.borderSubtle}`,
        borderRadius: "10px",
      }}
    >
      <div className="flex items-center gap-3 min-w-0">
        <div
          className="w-1.5 h-1.5 rounded-full shrink-0"
          style={{ background: repo.is_active ? C.online : C.textDim }}
        />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="font-medium text-sm truncate font-mono min-w-0" style={{ color: C.textPrimary }}>
              {repo.full_name}
            </span>
            <VisibilityBadge visibility={repo.visibility} />
            {!repo.is_active && (
              <span
                className="shrink-0 uppercase"
                style={{
                  background: `${C.warning}1A`,
                  color: C.warning,
                  fontSize: "9px",
                  padding: "1px 5px",
                  borderRadius: "4px",
                  letterSpacing: "0.06em",
                }}
              >
                {t("archived")}
              </span>
            )}
          </div>
          {repo.description && (
            <p className="text-xs mt-0.5 truncate" style={{ color: C.textSecondary }}>
              {repo.description}
            </p>
          )}
          <div className="flex items-center gap-1.5 mt-1 flex-wrap">
            <span className="inline-flex items-center gap-1 text-xs font-mono" style={{ color: C.textMuted }}>
              <GitBranch size={10} />
              {repo.default_branch}
            </span>
            <span style={{ color: C.borderSubtle }}>·</span>
            {repo.rules_md ? (
              <span className="text-xs" style={{ color: C.accent }}>{t("rulesSet")}</span>
            ) : (
              <span className="text-xs" style={{ color: C.textDim }}>{t("noRules")}</span>
            )}
            <span style={{ color: C.borderSubtle }}>·</span>
            <span className="text-xs" style={{ color: C.textDim }}>
              {repo.last_synced_at ? t("synced", { ago: timeAgo(repo.last_synced_at, locale) }) : t("neverSynced")}
            </span>
          </div>
          {shownProjects.length > 0 && (
            <div className="flex items-center gap-1.5 mt-1.5 flex-wrap">
              {shownProjects.map((p) => (
                <span
                  key={p.id}
                  className="inline-flex items-center px-1.5 py-0.5 rounded-md font-mono text-[10px]"
                  style={{
                    backgroundColor: C.accentSubtle,
                    color: C.textSecondary,
                    border: `1px solid ${C.borderAccent}`,
                  }}
                >
                  {p.name}
                </span>
              ))}
              {restCount > 0 && (
                <span className="text-[10px]" style={{ color: C.textMuted }}>+{restCount}</span>
              )}
            </div>
          )}
        </div>
      </div>
    </motion.button>
  );
}

// ── GitHub onboarding banner ───────────────────────────────────────────────────

function GithubOnboardingBanner() {
  const t = useTranslations("repos");
  return (
    <div
      className="flex items-center justify-between gap-3 px-4 py-3 mb-4 rounded-xl"
      style={{ background: C.accentSubtle, border: `1px solid ${C.borderAccent}` }}
    >
      <div className="flex items-center gap-3 min-w-0">
        <Github size={16} style={{ color: C.accent }} className="shrink-0" />
        <p className="text-xs min-w-0" style={{ color: C.textSecondary }}>
          {t("githubBanner")}
        </p>
      </div>
      <Link
        href="/settings?section=github"
        className="flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-lg transition-all cursor-pointer shrink-0"
        style={{ background: C.accentSubtle, border: `1px solid ${C.borderAccent}`, color: C.accent }}
      >
        {t("connectGithub")}
      </Link>
    </div>
  );
}

// ── Main Page ─────────────────────────────────────────────────────────────────

export default function ReposPage() {
  const t = useTranslations("repos");
  const [includeInactive, setIncludeInactive] = useState(false);
  const [selectedRepoId, setSelectedRepoId] = useState<string | null>(null);
  const [importOpen, setImportOpen] = useState(false);

  const { data: repos, isLoading } = useQuery<Repo[]>({
    queryKey: ["repos", includeInactive],
    queryFn: () => api.repos.list(includeInactive),
  });

  const { data: githubStatus } = useQuery<GithubStatus>({
    queryKey: ["github-status"],
    queryFn: () => api.repos.githubStatus(),
    staleTime: 30_000,
  });

  const list = repos ?? [];

  return (
    <AppShell>
      <div className="py-6 max-w-4xl">
        {/* Header */}
        <div className="flex items-center justify-between gap-3 mb-6">
          <div>
            <div className="label-sys mb-2">Code · Repos</div>
            <h1 className="display text-xl font-semibold" style={{ color: C.textPrimary }}>
              {t("title")}
            </h1>
            <p className="text-[13px] mt-0.5" style={{ color: C.textSecondary }}>
              {t("subtitle")}
            </p>
          </div>
          <button
            onClick={() => setImportOpen(true)}
            className="flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-lg transition-all cursor-pointer shrink-0"
            style={{
              background: C.accentSubtle,
              border: `1px solid ${C.borderAccent}`,
              color: C.accent,
            }}
          >
            <Plus size={11} />
            {t("importRepo")}
          </button>
        </div>

        {githubStatus && !githubStatus.configured && <GithubOnboardingBanner />}

        {/* Include-archived toggle */}
        <label
          className="flex items-center gap-2 text-xs mb-4 cursor-pointer w-fit"
          style={{ color: C.textMuted }}
        >
          <input
            type="checkbox"
            checked={includeInactive}
            onChange={(e) => setIncludeInactive(e.target.checked)}
            style={{ accentColor: C.accent }}
          />
          {t("showArchived")}
        </label>

        {isLoading && (
          <div className="flex items-center gap-2 py-2" style={{ color: C.textMuted }}>
            <Loader2 size={13} className="animate-spin" />
            <span className="text-xs">{t("loading")}</span>
          </div>
        )}

        {!isLoading && list.length === 0 && (
          <div
            className="flex flex-col items-center gap-3 text-center py-16 rounded-xl"
            style={{ border: `1px dashed ${C.border}` }}
          >
            <FolderGit2 size={28} style={{ color: C.textDim }} />
            <div>
              <p className="text-sm font-medium" style={{ color: C.textSecondary }}>
                {t("emptyTitle")}
              </p>
              <p className="text-xs mt-1" style={{ color: C.textMuted }}>
                {t("emptyHint")}
              </p>
            </div>
            <button
              onClick={() => setImportOpen(true)}
              className="flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-lg transition-all cursor-pointer mt-1"
              style={{
                background: C.accentSubtle,
                border: `1px solid ${C.borderAccent}`,
                color: C.accent,
              }}
            >
              <Plus size={11} />
              {t("importRepo")}
            </button>
          </div>
        )}

        {list.length > 0 && (
          <div className="flex flex-col gap-2">
            {list.map((repo) => (
              <RepoCard key={repo.id} repo={repo} onClick={() => setSelectedRepoId(repo.id)} />
            ))}
          </div>
        )}
      </div>

      <RepoDetailPanel
        repoId={selectedRepoId}
        open={selectedRepoId !== null}
        onClose={() => setSelectedRepoId(null)}
      />

      <ImportRepoDialog open={importOpen} onClose={() => setImportOpen(false)} />
    </AppShell>
  );
}
