"use client";

/**
 * DiffPanel — the chat's "Changes" side panel (label `sessions.panels.diff`).
 *
 * Three tabs, phone first:
 *  - **This chat** (default): every file THIS chat session changed, from the
 *    per-edit diffs in its transcript (`GET /agents/{id}/chat/changes`,
 *    `ChatChangesList`). Any harness, with or without git.
 *  - **Uncommitted** / **Last commit**: a structured git diff of the repository
 *    the chat works in (`GET /agents/{id}/chat/diff?scope=`) — shown ONLY when
 *    there is one (`changes.repo`). A chat in a plain folder gets a quiet line
 *    saying so, never another workspace.
 *
 * WHICH repository is the backend's call (`workspace_diff.choose_repo`:
 * running task → the chat session's own folder; nothing is guessed). The
 * `repo` block names it in one line under the tabs — repo, branch,
 * ahead/behind and why. Until 12.10.2026 a chat in `/workspace` (no repo) was
 * shown the freshest of ~70 old task worktrees, an unrelated benchmark.
 *
 * Refresh: every 15s while the parent-supplied `refreshHot` is true (the chat
 * stream's `status === "working"`), and once more the moment it turns false —
 * the end of a turn is exactly when edits and commits land. A manual refresh
 * button is always there.
 *
 * Every visible string comes from `sessions.diff.*` (EN/DE); the backend
 * sends codes (`scope`, `source.kind`) and ISO times, never sentences. The
 * tab choice persists in localStorage ("mc.chat.changestab").
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
import { ChatChangesList } from "./ChatChangesList";

type DiffScope = "worktree" | "last-commit";
type ChangesTab = "chat" | DiffScope;

const TAB_STORAGE_KEY = "mc.chat.changestab";

const TABS: { key: ChangesTab; labelKey: "tabChat" | "scopeWorktree" | "scopeLastCommit" }[] = [
  { key: "chat", labelKey: "tabChat" },
  { key: "worktree", labelKey: "scopeWorktree" },
  { key: "last-commit", labelKey: "scopeLastCommit" },
];

function loadTab(): ChangesTab {
  try {
    const v = localStorage.getItem(TAB_STORAGE_KEY);
    return v === "worktree" || v === "last-commit" ? v : "chat";
  } catch {
    return "chat";
  }
}

function saveTab(tab: ChangesTab) {
  try {
    localStorage.setItem(TAB_STORAGE_KEY, tab);
  } catch {}
}

// The backend's 404 body is `{"reason": "no_workspace"}` (agent_chat.py) —
// `request()` throws `Error("API 404: " + <raw body text>)`, so the reason
// string survives as a substring the same way `isNoTranscriptError` keys on
// "no_transcript" (chatTypes.ts).
function isNoWorkspaceError(err: unknown): boolean {
  return err instanceof Error && err.message.includes("no_workspace");
}

/** Repo · branch · ahead/behind · why — one quiet line, machine values in
 *  mono (K7). Ahead/behind only when non-zero (K3: nothing empty). */
function SourceLine({ source }: { source: ChatDiffSource }) {
  const t = useTranslations("sessions.diff");
  const ahead = source.ahead ?? 0;
  const behind = source.behind ?? 0;
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
      {(ahead > 0 || behind > 0) && (
        <span data-testid="diff-ahead-behind" className="font-mono tabular-nums shrink-0">
          {ahead > 0 && <span title={t("ahead", { count: ahead })}>{`↑${ahead}`}</span>}
          {ahead > 0 && behind > 0 && " "}
          {behind > 0 && <span title={t("behind", { count: behind })}>{`↓${behind}`}</span>}
        </span>
      )}
      {/* A kind a newer backend invents shows nothing rather than a raw key. */}
      {t.has(`kind.${source.kind}`) && <span className="ml-auto shrink-0">{t(`kind.${source.kind}`)}</span>}
    </div>
  );
}

/** The chat's folder is no git repository — said once, quietly, instead of
 *  the repo line (never another workspace in its place). */
function NoRepoLine() {
  const t = useTranslations("sessions.diff");
  return (
    <div
      data-testid="diff-no-repo"
      className="flex items-center gap-2 px-3 py-2 border-b text-xs min-w-0 shrink-0"
      style={{ borderColor: C.borderSubtle, color: C.textMuted }}
    >
      <FolderX size={12} className="shrink-0" aria-hidden="true" />
      <span className="min-w-0">{t("noRepo")}</span>
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
  const [tab, setTabState] = useState<ChangesTab>("chat");

  // Same SSR-safe restore pattern as sessions/page.tsx's persisted state:
  // localStorage doesn't exist during the server render, so the real value
  // is read after mount.
  useEffect(() => {
    setTabState(loadTab());
  }, []);

  function setTab(next: ChangesTab) {
    setTabState(next);
    saveTab(next);
  }

  const changesQuery = useQuery({
    queryKey: ["chat-changes", agentId],
    queryFn: () => api.chat.changes(agentId),
    refetchInterval: refreshHot ? 15_000 : false,
  });
  const repo = changesQuery.data?.repo ?? null;
  // Git tabs exist only for a known repository; a remembered git tab falls
  // back to "This chat" when the chat has none.
  const active: ChangesTab = repo ? tab : "chat";
  const scope: DiffScope | null = active === "chat" ? null : active;

  const diffQuery = useQuery({
    queryKey: ["chat-diff", agentId, scope],
    queryFn: () => api.chat.diff(agentId, scope ?? "worktree"),
    enabled: scope !== null,
    refetchInterval: refreshHot && scope !== null ? 15_000 : false,
  });

  // Turn end (working → anything else): refetch the list and both git views
  // — edits and a commit made in the last seconds of a turn otherwise waited
  // for a poll that no longer runs.
  const wasHot = useRef(refreshHot);
  useEffect(() => {
    if (wasHot.current && !refreshHot) {
      void queryClient.invalidateQueries({ queryKey: ["chat-changes", agentId] });
      void queryClient.invalidateQueries({ queryKey: ["chat-diff", agentId] });
    }
    wasHot.current = refreshHot;
  }, [refreshHot, agentId, queryClient]);

  const isFetching = changesQuery.isFetching || (scope !== null && diffQuery.isFetching);
  function refresh() {
    void changesQuery.refetch();
    if (scope !== null) void diffQuery.refetch();
  }

  const diff: CommitDiff | undefined = diffQuery.data;
  const noWorkspace = diffQuery.isError && isNoWorkspaceError(diffQuery.error);
  const isCommit = scope === "last-commit";
  const commitMeta =
    isCommit && diff?.hash
      ? diff.committed_at
        ? t("commitMeta", { hash: diff.hash, ago: timeAgo(diff.committed_at, locale) })
        : diff.hash
      : null;

  return (
    <div className="flex flex-col h-full min-h-0">
      {/* Header: tabs (git tabs only with a repository) + manual refresh */}
      <div className="flex items-center gap-2 px-3 py-2 border-b shrink-0" style={{ borderColor: C.border }}>
        {repo ? (
          <div
            role="tablist"
            aria-label={t("scopeLabel")}
            className="flex items-center rounded-md overflow-hidden min-w-0"
            style={{ border: `1px solid ${C.border}` }}
          >
            {TABS.map(({ key, labelKey }) => (
              <button
                key={key}
                type="button"
                role="tab"
                onClick={() => setTab(key)}
                aria-selected={active === key}
                // Phone: 12 px so three tabs + refresh fit 393 px in both
                // languages without clipping, and 44 px tall (K11 targets);
                // the sheet title stays the loudest text (K1).
                className="px-3 py-2 min-h-11 md:min-h-0 text-xs md:text-sm font-medium transition-colors cursor-pointer whitespace-nowrap"
                style={{
                  background: active === key ? C.accentSubtle : "transparent",
                  color: active === key ? C.accent : C.textMuted,
                  borderRight: key !== "last-commit" ? `1px solid ${C.border}` : undefined,
                }}
              >
                {t(labelKey)}
              </button>
            ))}
          </div>
        ) : (
          <span className="text-sm font-medium" style={{ color: C.textSecondary }}>
            {t("tabChat")}
          </span>
        )}

        <button
          type="button"
          onClick={refresh}
          disabled={isFetching}
          aria-label={t("refresh")}
          title={t("refresh")}
          className="ml-auto shrink-0 flex items-center justify-center w-11 h-11 md:w-8 md:h-8 rounded-md transition-colors disabled:opacity-40 cursor-pointer"
          style={{ border: `1px solid ${C.border}`, color: C.textSecondary }}
        >
          <RefreshCw size={12} className={isFetching ? "animate-spin" : ""} />
        </button>
      </div>

      {repo ? <SourceLine source={repo} /> : changesQuery.isSuccess && <NoRepoLine />}

      {/* Body */}
      <div className="flex-1 min-h-0 overflow-y-auto">
        {scope === null ? (
          changesQuery.isLoading ? (
            <Spinner />
          ) : changesQuery.isError || !changesQuery.data ? (
            <Message text={t("chatLoadFailed")} />
          ) : (
            <ChatChangesList key={agentId} changes={changesQuery.data} />
          )
        ) : diffQuery.isLoading ? (
          <Spinner />
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
        ) : diffQuery.isError ? (
          <Message text={t("loadFailed")} />
        ) : !diff || diff.files.length === 0 ? (
          <Message text={isCommit && !diff?.hash ? t("noCommit") : t("noChanges")} />
        ) : (
          <GitDiffView diff={diff} subtitle={commitMeta} />
        )}
      </div>
    </div>
  );
}

function Spinner() {
  return (
    <div className="flex items-center justify-center h-full min-h-40">
      <Loader2 size={16} className="animate-spin" style={{ color: C.textMuted }} />
    </div>
  );
}

function Message({ text }: { text: string }) {
  return (
    <div className="flex items-center justify-center h-full min-h-40 px-6 text-center">
      <p className="text-sm max-w-xs" style={{ color: C.textMuted }}>
        {text}
      </p>
    </div>
  );
}
