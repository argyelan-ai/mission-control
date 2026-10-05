"use client";

/**
 * DiffPanel — side panel showing a structured git diff of the repository the
 * selected agent is working in: uncommitted changes incl. new files (scope
 * `worktree`, default) or the most recent commit (scope `last-commit`) —
 * mirrors `GET /agents/{id}/chat/diff?scope=`.
 *
 * WHICH repository is the backend's call (`workspace_diff.choose_repo`:
 * running task → the chat session's own folder → the repository git touched
 * last). The payload's `source` block names it, and this panel shows it in a
 * line under the tabs — repo, branch and why — so the operator can always
 * tell what they are looking at. Before 04.10.2026 the panel showed a
 * months-old commit of a finished task with no hint where it came from.
 *
 * Refresh: every 15s while the parent-supplied `refreshHot` is true (the chat
 * stream's `status === "working"`), and once more the moment it turns false —
 * the end of a turn is exactly when an agent's commit lands, and the poll
 * alone could miss it by up to 15s. DiffPanel knows nothing about chat state
 * beyond that plain boolean (same separation ChatView keeps from PanelRail).
 * A manual refresh button is always there.
 *
 * Every visible string comes from `sessions.diff.*` (EN/DE); the backend
 * sends codes (`scope`, `source.kind`) and an ISO time, never sentences.
 * Scope choice persists in localStorage ("mc.chat.diffscope").
 */
import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useLocale, useTranslations } from "next-intl";
import { RefreshCw, Loader2, FolderX, FolderGit2 } from "lucide-react";
import { api } from "@/lib/api";
import { C } from "@/lib/colors";
import { timeAgo } from "@/lib/utils";
import type { ChatDiffSource, CommitDiff } from "@/lib/types";
import { GitDiffView } from "@/components/git/GitDiffView";

type DiffScope = "worktree" | "last-commit";

const SCOPE_STORAGE_KEY = "mc.chat.diffscope";

const SCOPES: { key: DiffScope; labelKey: "scopeWorktree" | "scopeLastCommit" }[] = [
  { key: "worktree", labelKey: "scopeWorktree" },
  { key: "last-commit", labelKey: "scopeLastCommit" },
];

function loadScope(): DiffScope {
  try {
    return localStorage.getItem(SCOPE_STORAGE_KEY) === "last-commit" ? "last-commit" : "worktree";
  } catch {
    return "worktree";
  }
}

function saveScope(scope: DiffScope) {
  try {
    localStorage.setItem(SCOPE_STORAGE_KEY, scope);
  } catch {}
}

// The backend's 404 body is `{"reason": "no_workspace"}` (agent_chat.py) —
// `request()` throws `Error("API 404: " + <raw body text>)`, so the reason
// string survives as a substring the same way `isNoTranscriptError` keys on
// "no_transcript" (chatTypes.ts).
function isNoWorkspaceError(err: unknown): boolean {
  return err instanceof Error && err.message.includes("no_workspace");
}

/** Repo · branch · why — one quiet line, machine values in mono (K7). */
function SourceLine({ source }: { source: ChatDiffSource }) {
  const t = useTranslations("sessions.diff");
  return (
    <div
      data-testid="diff-source"
      title={source.path}
      className="flex items-center gap-2 px-3 py-2 border-b text-xs min-w-0 shrink-0"
      style={{ borderColor: C.borderSubtle, color: C.textMuted }}
    >
      <FolderGit2 size={12} className="shrink-0" />
      <span className="font-mono truncate min-w-0" style={{ color: C.textSecondary }}>
        {source.repo}
        {source.branch && <span style={{ color: C.textMuted }}>{` · ${source.branch}`}</span>}
      </span>
      {/* A kind a newer backend invents shows nothing rather than a raw key. */}
      {t.has(`kind.${source.kind}`) && <span className="ml-auto shrink-0">{t(`kind.${source.kind}`)}</span>}
    </div>
  );
}

interface DiffPanelProps {
  agentId: string;
  /** True while the chat stream is actively working — enables the 15s
   *  auto-refetch; its fall to false refetches once (turn end). Defaults to
   *  false (no polling) so a standalone render never spins up a timer nobody
   *  asked for. */
  refreshHot?: boolean;
}

export function DiffPanel({ agentId, refreshHot = false }: DiffPanelProps) {
  const t = useTranslations("sessions.diff");
  const locale = useLocale();
  const queryClient = useQueryClient();
  const [scope, setScopeState] = useState<DiffScope>("worktree");

  // Same SSR-safe restore pattern as sessions/page.tsx's persisted state:
  // localStorage doesn't exist during the server render, so the real value
  // is read after mount.
  useEffect(() => {
    setScopeState(loadScope());
  }, []);

  function setScope(next: DiffScope) {
    setScopeState(next);
    saveScope(next);
  }

  const { data, isLoading, isFetching, isError, error, refetch } = useQuery({
    queryKey: ["chat-diff", agentId, scope],
    queryFn: () => api.chat.diff(agentId, scope),
    refetchInterval: refreshHot ? 15_000 : false,
  });

  // Turn end (working → anything else): refetch BOTH scopes' data — the
  // visible one right away, the other on its next view. A commit made in the
  // last seconds of a turn otherwise waited for a poll that no longer runs.
  const wasHot = useRef(refreshHot);
  useEffect(() => {
    if (wasHot.current && !refreshHot) {
      void queryClient.invalidateQueries({ queryKey: ["chat-diff", agentId] });
    }
    wasHot.current = refreshHot;
  }, [refreshHot, agentId, queryClient]);

  const noWorkspace = isError && isNoWorkspaceError(error);
  const diff: CommitDiff | undefined = data;
  const isCommit = scope === "last-commit";
  const commitMeta =
    isCommit && diff?.hash
      ? diff.committed_at
        ? t("commitMeta", { hash: diff.hash, ago: timeAgo(diff.committed_at, locale) })
        : diff.hash
      : null;

  return (
    <div className="flex flex-col h-full min-h-0">
      {/* Header: scope switch + manual refresh */}
      <div className="flex items-center gap-2 px-3 py-2 border-b shrink-0" style={{ borderColor: C.border }}>
        <div
          role="tablist"
          aria-label={t("scopeLabel")}
          className="flex items-center rounded-md overflow-hidden"
          style={{ border: `1px solid ${C.border}` }}
        >
          {SCOPES.map(({ key, labelKey }) => (
            <button
              key={key}
              type="button"
              role="tab"
              onClick={() => setScope(key)}
              aria-selected={scope === key}
              className="px-3 py-2 text-sm font-medium transition-colors cursor-pointer whitespace-nowrap"
              style={{
                background: scope === key ? C.accentSubtle : "transparent",
                color: scope === key ? C.accent : C.textMuted,
                borderRight: key !== "last-commit" ? `1px solid ${C.border}` : undefined,
              }}
            >
              {t(labelKey)}
            </button>
          ))}
        </div>

        <button
          type="button"
          onClick={() => refetch()}
          disabled={isFetching}
          aria-label={t("refresh")}
          title={t("refresh")}
          className="ml-auto flex items-center justify-center w-11 h-11 md:w-8 md:h-8 rounded-md transition-colors disabled:opacity-40 cursor-pointer"
          style={{ border: `1px solid ${C.border}`, color: C.textSecondary }}
        >
          <RefreshCw size={12} className={isFetching ? "animate-spin" : ""} />
        </button>
      </div>

      {diff?.source && !isError && <SourceLine source={diff.source} />}

      {/* Body */}
      <div className="flex-1 min-h-0 overflow-y-auto">
        {isLoading ? (
          <div className="flex items-center justify-center h-full min-h-40">
            <Loader2 size={16} className="animate-spin" style={{ color: C.textMuted }} />
          </div>
        ) : noWorkspace ? (
          <div className="flex flex-col items-center justify-center h-full min-h-40 gap-2 px-6 text-center">
            <FolderX size={24} style={{ color: C.textMuted, opacity: 0.4 }} />
            <p className="text-sm" style={{ color: C.textSecondary }}>
              {t("noRepo")}
            </p>
            <p className="text-xs max-w-xs" style={{ color: C.textMuted }}>
              {t("noRepoHint")}
            </p>
          </div>
        ) : isError ? (
          <div className="flex flex-col items-center justify-center h-full min-h-40 gap-2 px-6 text-center">
            <p className="text-sm max-w-xs" style={{ color: C.textMuted }}>
              {t("loadFailed")}
            </p>
          </div>
        ) : !diff || diff.files.length === 0 ? (
          <div className="flex items-center justify-center h-full min-h-40">
            <p className="text-sm" style={{ color: C.textMuted }}>
              {isCommit && !diff?.hash ? t("noCommit") : t("noChanges")}
            </p>
          </div>
        ) : (
          <GitDiffView diff={diff} subtitle={commitMeta} />
        )}
      </div>
    </div>
  );
}
