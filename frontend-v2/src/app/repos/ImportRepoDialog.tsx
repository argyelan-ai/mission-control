"use client";

import { useState } from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, Github, Loader2, Lock, Search, Globe2 } from "lucide-react";
import { ResponsiveModal } from "@/components/shared/ResponsiveModal";
import { api } from "@/lib/api";
import { notify } from "@/lib/notify";
import { C, STATUS_TEXT } from "@/lib/colors";
import { timeAgo } from "@/lib/utils";
import type { GithubStatus, RepoImportCandidate } from "@/lib/types";

interface ImportRepoDialogProps {
  open: boolean;
  onClose: () => void;
}

/** Best-effort extraction of the FastAPI `detail` message from a failed
 * `request()` call — its Error.message is `API <status>: <raw body>`. */
function extractErrorDetail(err: unknown): string | null {
  if (!(err instanceof Error)) return null;
  const match = err.message.match(/^API \d+: ([\s\S]*)$/);
  const body = match ? match[1] : err.message;
  try {
    const parsed = JSON.parse(body);
    if (typeof parsed?.detail === "string") return parsed.detail;
  } catch {
    // not JSON — fall through to the raw text
  }
  return body || null;
}

/**
 * Registers an existing GitHub repo in MC (POST /repos). Candidates are the
 * account's GitHub repos not yet in the MC registry — fetched only while the
 * dialog is open (each fetch is a live `gh repo list` call).
 */
export function ImportRepoDialog({ open, onClose }: ImportRepoDialogProps) {
  const queryClient = useQueryClient();
  const [search, setSearch] = useState("");
  const [imported, setImported] = useState<Set<string>>(new Set());
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const { data: candidates, isLoading, error } = useQuery<RepoImportCandidate[]>({
    queryKey: ["repo-import-candidates"],
    queryFn: api.repos.importCandidates,
    enabled: open,
  });

  // Shares the ["github-status"] cache with ReposPage/Settings — used to tell
  // "GitHub isn't connected" apart from a generic candidates-fetch failure.
  const { data: githubStatus } = useQuery<GithubStatus>({
    queryKey: ["github-status"],
    queryFn: () => api.repos.githubStatus(),
    enabled: open,
    staleTime: 30_000,
  });

  const importMutation = useMutation({
    mutationFn: (fullName: string) => api.repos.register(fullName),
    onSuccess: (repo) => {
      setImported((prev) => new Set(prev).add(repo.full_name));
      queryClient.invalidateQueries({ queryKey: ["repos"] });
      notify.success(`${repo.full_name} imported`);
    },
    onError: (err) => {
      setErrorMsg(err instanceof Error ? err.message : "Import failed");
    },
  });

  const handleClose = () => {
    setSearch("");
    setErrorMsg(null);
    onClose();
  };

  const filtered = (candidates ?? []).filter((c) =>
    c.full_name.toLowerCase().includes(search.toLowerCase())
  );

  return (
    <ResponsiveModal open={open} onClose={handleClose} aria-labelledby="import-repo-title">
      <div className="px-5 pt-4 pb-3 shrink-0" style={{ borderBottom: `1px solid ${C.borderSubtle}` }}>
        <h2 id="import-repo-title" className="text-base font-semibold" style={{ color: C.textPrimary }}>
          Import repo
        </h2>
        <p className="text-xs mt-1" style={{ color: C.textMuted }}>
          Add an existing GitHub repo to the MC registry.
        </p>
      </div>

      <div className="px-5 pt-3">
        <div
          className="flex items-center gap-2 px-3 py-2 rounded-lg"
          style={{ background: C.bgDeep, border: `1px solid ${C.border}` }}
        >
          <Search size={13} style={{ color: C.textMuted }} />
          <input
            autoFocus
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search repos..."
            aria-label="Search repos"
            className="flex-1 bg-transparent text-sm outline-none"
            style={{ color: C.textPrimary }}
          />
        </div>
      </div>

      <div className="px-5 py-3 overflow-y-auto flex-1" style={{ maxHeight: "50vh" }}>
        {isLoading && (
          <div className="flex items-center gap-2 py-6 justify-center text-xs" style={{ color: C.textMuted }}>
            <Loader2 size={13} className="animate-spin" /> Loading GitHub repos...
          </div>
        )}

        {error && githubStatus && !githubStatus.configured ? (
          <div
            className="flex flex-col gap-2 text-xs px-3 py-3 rounded-lg"
            style={{ background: C.accentSubtle, border: `1px solid ${C.borderAccent}` }}
          >
            <div className="flex items-center gap-2" style={{ color: C.textPrimary }}>
              <Github size={13} style={{ color: C.accent }} />
              GitHub is not connected yet.
            </div>
            <p style={{ color: C.textMuted }}>
              Connect a GitHub owner + token to list and import repos.
            </p>
            <Link href="/settings?section=github" className="w-fit font-medium" style={{ color: C.accent }}>
              Connect GitHub →
            </Link>
          </div>
        ) : error ? (
          <div
            className="text-xs px-3 py-2 rounded-lg"
            style={{ background: `${C.error}14`, border: `1px solid ${C.error}33`, color: STATUS_TEXT.error }}
          >
            <p>Could not load GitHub repos.</p>
            {extractErrorDetail(error) && (
              <p className="mt-1 opacity-80">{extractErrorDetail(error)}</p>
            )}
          </div>
        ) : null}

        {!isLoading && !error && filtered.length === 0 && (
          <div className="text-xs text-center py-6" style={{ color: C.textMuted }}>
            {candidates?.length === 0
              ? "All repos are already registered."
              : `No results for "${search}"`}
          </div>
        )}

        <ul className="flex flex-col gap-1.5">
          {filtered.map((c) => {
            const isImported = imported.has(c.full_name);
            const isPending = importMutation.isPending && importMutation.variables === c.full_name;
            return (
              <li
                key={c.full_name}
                className="flex items-center justify-between gap-3 px-3 py-2.5 rounded-lg"
                style={{ background: C.borderSubtle, border: `1px solid ${C.borderSubtle}` }}
              >
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-1.5">
                    {c.visibility === "private" ? (
                      <Lock size={10} style={{ color: C.textMuted }} />
                    ) : (
                      <Globe2 size={10} style={{ color: C.textMuted }} />
                    )}
                    <span className="text-sm font-mono truncate" style={{ color: C.textPrimary }}>
                      {c.full_name}
                    </span>
                  </div>
                  {c.description && (
                    <p className="text-xs mt-0.5 truncate" style={{ color: C.textMuted }}>
                      {c.description}
                    </p>
                  )}
                  {c.pushed_at && (
                    <p className="text-[10px] mt-0.5" style={{ color: C.textDim }}>
                      Last pushed {timeAgo(c.pushed_at)}
                    </p>
                  )}
                </div>
                {isImported ? (
                  <span className="inline-flex items-center gap-1 text-xs shrink-0" style={{ color: C.online }}>
                    <CheckCircle2 size={12} /> Imported
                  </span>
                ) : (
                  <button
                    onClick={() => { setErrorMsg(null); importMutation.mutate(c.full_name); }}
                    disabled={isPending}
                    className="text-xs px-2.5 py-1 rounded-md cursor-pointer disabled:opacity-50 shrink-0 transition-all"
                    style={{ background: C.accentSubtle, border: `1px solid ${C.borderAccent}`, color: C.accent }}
                  >
                    {isPending ? <Loader2 size={11} className="animate-spin" /> : "Import"}
                  </button>
                )}
              </li>
            );
          })}
        </ul>

        {errorMsg && (
          <div
            className="mt-3 text-xs px-3 py-2 rounded-lg"
            style={{ background: `${C.error}14`, border: `1px solid ${C.error}33`, color: STATUS_TEXT.error }}
          >
            {errorMsg}
          </div>
        )}
      </div>

      <div
        className="flex items-center justify-end gap-2 px-5 py-3 shrink-0"
        style={{ borderTop: `1px solid ${C.borderSubtle}` }}
      >
        <button
          onClick={handleClose}
          className="px-3.5 py-2 rounded-lg text-sm font-medium transition-colors cursor-pointer"
          style={{ color: C.textSecondary, border: `1px solid ${C.border}` }}
        >
          Close
        </button>
      </div>
    </ResponsiveModal>
  );
}
