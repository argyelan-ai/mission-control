"use client";

/**
 * DiffPanel — Task C1. Side panel showing a structured git diff over the
 * selected agent's workspace: uncommitted changes ("Arbeitsstand", scope
 * `worktree`, default) or the most recent commit ("Letzter Commit", scope
 * `last-commit`) — mirrors `GET /agents/{id}/chat/diff?scope=`.
 *
 * Renders the existing `GitDiffView({ diff })` once a diff loads. Auto-
 * refetches every 15s while the parent-supplied `refreshHot` is true (the
 * chat stream's `state.status === "working"` — DiffPanel itself doesn't
 * know about chat state, it only reacts to the plain boolean the parent
 * derives, same separation ChatView keeps from PanelRail). A manual refresh
 * button is always available regardless of `refreshHot`.
 *
 * Scope choice persists in localStorage ("mc.chat.diffscope") — same
 * try/catch-wrapped pattern as sessions/page.tsx's other persisted toggles.
 */
import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { RefreshCw, Loader2, FolderX } from "lucide-react";
import { api } from "@/lib/api";
import { C } from "@/lib/colors";
import { GitDiffView } from "@/components/git/GitDiffView";

type DiffScope = "worktree" | "last-commit";

const SCOPE_STORAGE_KEY = "mc.chat.diffscope";

const SCOPES: { key: DiffScope; label: string }[] = [
  { key: "worktree", label: "Arbeitsstand" },
  { key: "last-commit", label: "Letzter Commit" },
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

interface DiffPanelProps {
  agentId: string;
  /** True while the chat stream is actively working — enables the 15s
   *  auto-refetch. Defaults to false (no polling) so a standalone render
   *  never spins up a timer nobody asked for. */
  refreshHot?: boolean;
}

export function DiffPanel({ agentId, refreshHot = false }: DiffPanelProps) {
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

  const noWorkspace = isError && isNoWorkspaceError(error);

  return (
    <div className="flex flex-col h-full min-h-0">
      {/* Header: scope switch + manual refresh */}
      <div className="flex items-center gap-2 px-3 py-2 border-b shrink-0" style={{ borderColor: C.border }}>
        <div
          role="tablist"
          aria-label="Diff-Bereich"
          className="flex items-center rounded-md overflow-hidden"
          style={{ border: `1px solid ${C.border}` }}
        >
          {SCOPES.map(({ key, label }) => (
            <button
              key={key}
              type="button"
              role="tab"
              onClick={() => setScope(key)}
              aria-selected={scope === key}
              className="px-2.5 py-1.5 text-[13px] font-medium transition-colors cursor-pointer whitespace-nowrap"
              style={{
                background: scope === key ? C.accentSubtle : "transparent",
                color: scope === key ? C.accent : C.textMuted,
                borderRight: key !== "last-commit" ? `1px solid ${C.border}` : undefined,
              }}
            >
              {label}
            </button>
          ))}
        </div>

        <button
          type="button"
          onClick={() => refetch()}
          disabled={isFetching}
          aria-label="Aktualisieren"
          title="Aktualisieren"
          className="ml-auto flex items-center justify-center w-7 h-7 rounded-md transition-colors disabled:opacity-40 cursor-pointer"
          style={{ border: `1px solid ${C.border}`, color: C.textSecondary }}
        >
          <RefreshCw size={12} className={isFetching ? "animate-spin" : ""} />
        </button>
      </div>

      {/* Body */}
      <div className="flex-1 min-h-0 overflow-y-auto">
        {isLoading ? (
          <div className="flex items-center justify-center h-full min-h-[160px]">
            <Loader2 size={16} className="animate-spin" style={{ color: C.textMuted }} />
          </div>
        ) : noWorkspace ? (
          <div className="flex flex-col items-center justify-center h-full min-h-[160px] gap-2 px-6 text-center">
            <FolderX size={24} style={{ color: C.textMuted, opacity: 0.4 }} />
            <p className="text-[13px]" style={{ color: C.textMuted }}>
              Kein Workspace
            </p>
          </div>
        ) : isError ? (
          <div className="flex flex-col items-center justify-center h-full min-h-[160px] gap-2 px-6 text-center">
            <p className="text-[13px] max-w-xs" style={{ color: C.textMuted }}>
              Diff konnte nicht geladen werden.
            </p>
          </div>
        ) : !data || data.files.length === 0 ? (
          <div className="flex items-center justify-center h-full min-h-[160px]">
            <p className="text-[13px]" style={{ color: C.textMuted }}>
              Keine Änderungen
            </p>
          </div>
        ) : (
          <GitDiffView diff={data} />
        )}
      </div>
    </div>
  );
}
