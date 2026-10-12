"use client";

/**
 * ChangesActions — the Changes panel's three write actions (Changes panel, part 3),
 * each behind ONE confirmation sheet (operator decision 12.10.2026: the chat is
 * the phone remote, every write needs one explicit tap):
 *
 *  - `RevertSheet`  — put one file this chat changed back to its state before
 *    the chat ("Revert app/prices.py? This undoes 3 edits by this chat.").
 *  - `CommitSheet`  — commit everything uncommitted with a suggested,
 *    editable message (nothing is pushed).
 *  - `PrSheet`      — push the branch and open a pull request; the done state
 *    carries the PR link.
 *
 * The backend decides and refuses (`services/chat_actions.py`); a refusal
 * arrives as `{"reason": code, "detail"?}` inside the error message and is
 * translated here (`sessions.diff.reason.*`) — an honest sentence, never a
 * raw error. Phone first: `ResponsiveModal` slides up from the bottom below
 * `sm`, one primary button per sheet (K11), the secondary is plain text.
 */
import { useEffect, useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { Check, ExternalLink, Loader2 } from "lucide-react";
import { api } from "@/lib/api";
import { C } from "@/lib/colors";
import { ResponsiveModal } from "@/components/shared/ResponsiveModal";

/** `{"reason": "...", "detail": "..."}` out of `request()`'s
 *  "API 409: {…}" message — `null` when the body is not ours. */
export function actionReason(err: unknown): { reason: string; detail?: string } | null {
  if (!(err instanceof Error)) return null;
  const i = err.message.indexOf("{");
  if (i < 0) return null;
  try {
    const body = JSON.parse(err.message.slice(i)) as { reason?: unknown; detail?: unknown };
    if (typeof body.reason !== "string") return null;
    return { reason: body.reason, detail: typeof body.detail === "string" ? body.detail : undefined };
  } catch {
    return null;
  }
}

function basename(path: string): string {
  const i = path.lastIndexOf("/");
  return i < 0 ? path : path.slice(i + 1);
}

/** Refusal codes `chat_actions.py` raises for a revert that will not
 *  succeed by tapping "Revert" again — nothing about the chat's folder, the
 *  file or what happened since changes by retrying. A sheet showing one of
 *  these drops the primary button down to "Close" only (the red sentence
 *  from `ErrorLine` already says why). Everything else — no parsed reason
 *  at all (a network error) or `agent_busy` ("try again when its turn
 *  ends") — stays retryable. */
const PERMANENT_REVERT_REFUSALS = new Set([
  "not_changed_by_chat",
  "path_outside",
  "changed_externally",
  "too_large",
  "cannot_restore",
  "unknown_original",
  "git_failed",
]);

/** After any action: the list, both git views and the action availability
 *  are stale. */
function useRefreshChanges(agentId: string) {
  const queryClient = useQueryClient();
  return () => {
    void queryClient.invalidateQueries({ queryKey: ["chat-changes", agentId] });
    void queryClient.invalidateQueries({ queryKey: ["chat-diff", agentId] });
  };
}

function ErrorLine({ error }: { error: unknown }) {
  const t = useTranslations("sessions.diff");
  const parsed = actionReason(error);
  const key = parsed && t.has(`reason.${parsed.reason}`) ? `reason.${parsed.reason}` : "reason.unknown";
  return (
    <div role="alert" data-testid="changes-action-error" className="flex flex-col gap-1">
      <p className="text-sm" style={{ color: C.error }}>
        {t(key)}
      </p>
      {parsed?.detail && (
        <p className="font-mono text-xs break-words" style={{ color: C.textMuted }}>
          {parsed.detail}
        </p>
      )}
    </div>
  );
}

interface SheetProps {
  open: boolean;
  title: string;
  onClose: () => void;
  children?: ReactNode;
  /** The primary button; omitted in the done state (then only Close). */
  confirmLabel?: string;
  onConfirm?: () => void;
  confirmDisabled?: boolean;
  busy?: boolean;
  testId: string;
}

/** The shared sheet: title, body, [Cancel] [Primary] — or [Close] when done. */
function ActionSheet({
  open,
  title,
  onClose,
  children,
  confirmLabel,
  onConfirm,
  confirmDisabled,
  busy,
  testId,
}: SheetProps) {
  const t = useTranslations("sessions.diff");
  return (
    <ResponsiveModal open={open} onClose={() => !busy && onClose()} aria-label={title} dismissOnOutside={!busy}>
      <div data-testid={testId} className="flex flex-col gap-4 px-4 pt-3 pb-4">
        <h2 className="text-base font-semibold break-words" style={{ color: C.textPrimary }}>
          {title}
        </h2>
        {children && <div className="flex flex-col gap-3 text-sm" style={{ color: C.textSecondary }}>{children}</div>}
        <div className="flex items-center justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            disabled={busy}
            className="min-h-11 px-4 rounded-md text-sm cursor-pointer bg-transparent border-0 disabled:opacity-50"
            style={{ color: C.textSecondary }}
          >
            {confirmLabel ? t("act.cancel") : t("act.close")}
          </button>
          {confirmLabel && (
            <button
              type="button"
              onClick={onConfirm}
              disabled={busy || confirmDisabled}
              data-testid={`${testId}-confirm`}
              className="min-h-11 px-4 rounded-md text-sm font-medium cursor-pointer border-0 flex items-center gap-2 disabled:opacity-50 disabled:cursor-not-allowed"
              style={{ background: C.accent, color: C.onAccent }}
            >
              {busy && <Loader2 size={14} className="animate-spin" aria-hidden="true" />}
              {confirmLabel}
            </button>
          )}
        </div>
      </div>
    </ResponsiveModal>
  );
}

function DoneLine({ children }: { children: ReactNode }) {
  return (
    <p data-testid="changes-action-done" className="flex items-start gap-2 text-sm" style={{ color: C.textPrimary }}>
      <Check size={16} className="shrink-0 mt-1" style={{ color: C.online }} aria-hidden="true" />
      <span className="min-w-0 break-words">{children}</span>
    </p>
  );
}

// ── Revert ───────────────────────────────────────────────────────────────────

export interface RevertTarget {
  /** The path as the Changes list names it (sent back to the server as is). */
  path: string;
  edits: number;
  created: boolean;
}

export function RevertSheet({
  agentId,
  target,
  onClose,
}: {
  agentId: string;
  target: RevertTarget | null;
  onClose: () => void;
}) {
  const t = useTranslations("sessions.diff");
  const refresh = useRefreshChanges(agentId);
  const mutation = useMutation({
    mutationFn: (path: string) => api.chat.revertFile(agentId, path),
    onSuccess: refresh,
  });
  const { reset } = mutation;
  useEffect(() => {
    if (target) reset();
  }, [target, reset]);

  const name = target ? basename(target.path) : "";
  const done = mutation.data;
  const refusal = mutation.isError ? actionReason(mutation.error) : null;
  const permanentRefusal = refusal !== null && PERMANENT_REVERT_REFUSALS.has(refusal.reason);
  return (
    <ActionSheet
      open={target !== null}
      testId="revert-sheet"
      title={done ? t("act.revertedTitle", { file: name }) : t("act.revertTitle", { file: target?.path ?? "" })}
      onClose={onClose}
      busy={mutation.isPending}
      confirmLabel={done || permanentRefusal ? undefined : t("act.revert")}
      onConfirm={() => target && mutation.mutate(target.path)}
    >
      {done ? (
        <DoneLine>
          {done.deleted
            ? t("act.revertDeleted", { file: name })
            : done.method === "git"
              ? t("act.revertGit", { file: name })
              : t("act.revertDone", { file: name })}
        </DoneLine>
      ) : (
        target && (
          <p>
            {target.created
              ? t("act.revertBodyCreated", { count: target.edits })
              : t("act.revertBody", { count: target.edits })}
          </p>
        )
      )}
      {mutation.isError && <ErrorLine error={mutation.error} />}
    </ActionSheet>
  );
}

// ── Commit ───────────────────────────────────────────────────────────────────

export function CommitSheet({ agentId, open, onClose }: { agentId: string; open: boolean; onClose: () => void }) {
  const t = useTranslations("sessions.diff");
  const refresh = useRefreshChanges(agentId);
  const draft = useQuery({
    queryKey: ["chat-commit-draft", agentId],
    queryFn: () => api.chat.commitDraft(agentId),
    enabled: open,
    staleTime: 0,
    gcTime: 0,
    retry: false,
  });
  const [message, setMessage] = useState("");
  const mutation = useMutation({
    mutationFn: (msg: string) => api.chat.commit(agentId, msg),
    onSuccess: refresh,
  });
  const { reset } = mutation;
  useEffect(() => {
    if (open) reset();
  }, [open, reset]);
  // The suggestion fills the field once per opening; the operator's edits win.
  const suggested = draft.data?.message;
  useEffect(() => {
    if (open && suggested !== undefined) setMessage(suggested);
  }, [open, suggested]);

  const done = mutation.data;
  const files = draft.data?.files.length ?? 0;
  const title = done
    ? t("act.committedTitle")
    : draft.data
      ? t("act.commitTitle", { count: files, branch: draft.data.branch })
      : t("act.commit");
  return (
    <ActionSheet
      open={open}
      testId="commit-sheet"
      title={title}
      onClose={onClose}
      busy={mutation.isPending}
      confirmLabel={done || !draft.data ? undefined : t("act.commit")}
      confirmDisabled={!message.trim() || files === 0}
      onConfirm={() => mutation.mutate(message)}
    >
      {done ? (
        <DoneLine>
          {done.status === "committed" && done.sha
            ? t("act.committed", { sha: done.sha, branch: done.branch })
            : t("reason.nothing_to_commit")}
        </DoneLine>
      ) : draft.isLoading ? (
        <Loader2 size={16} className="animate-spin" style={{ color: C.textMuted }} aria-label={t("act.loading")} />
      ) : draft.isError ? (
        <ErrorLine error={draft.error} />
      ) : (
        <>
          <label className="flex flex-col gap-2">
            <span className="text-xs" style={{ color: C.textMuted }}>
              {t("act.commitMessage")}
            </span>
            <textarea
              data-testid="commit-message"
              value={message}
              onChange={(e) => setMessage(e.target.value)}
              rows={6}
              spellCheck={false}
              // 16 px on phones: iOS zooms into smaller inputs.
              className="w-full px-3 py-2 rounded-md font-mono text-base md:text-sm resize-y outline-none"
              style={{ background: C.bgDeep, color: C.textPrimary, border: `1px solid ${C.border}` }}
            />
          </label>
          <p className="text-xs" style={{ color: C.textMuted }}>
            {t("act.commitHint")}
          </p>
        </>
      )}
      {mutation.isError && <ErrorLine error={mutation.error} />}
    </ActionSheet>
  );
}

// ── Pull request ─────────────────────────────────────────────────────────────

export function PrSheet({
  agentId,
  open,
  onClose,
  onOpened,
}: {
  agentId: string;
  open: boolean;
  onClose: () => void;
  /** The PR link, for the panel to keep showing after the sheet closes. */
  onOpened?: (url: string) => void;
}) {
  const t = useTranslations("sessions.diff");
  const refresh = useRefreshChanges(agentId);
  const draft = useQuery({
    queryKey: ["chat-pr-draft", agentId],
    queryFn: () => api.chat.prDraft(agentId),
    enabled: open,
    staleTime: 0,
    gcTime: 0,
    retry: false,
  });
  const [title, setTitle] = useState("");
  const mutation = useMutation({
    mutationFn: (value: string) => api.chat.openPr(agentId, value),
    onSuccess: (res) => {
      refresh();
      onOpened?.(res.url);
    },
  });
  const { reset } = mutation;
  useEffect(() => {
    if (open) reset();
  }, [open, reset]);
  const suggested = draft.data?.title;
  useEffect(() => {
    if (open && suggested !== undefined) setTitle(suggested);
  }, [open, suggested]);

  const done = mutation.data;
  const d = draft.data;
  return (
    <ActionSheet
      open={open}
      testId="pr-sheet"
      title={done ? (done.status === "exists" ? t("act.prExists") : t("act.prOpened")) : t("act.prTitle")}
      onClose={onClose}
      busy={mutation.isPending}
      confirmLabel={done || !d ? undefined : t("act.openPr")}
      confirmDisabled={!title.trim()}
      onConfirm={() => mutation.mutate(title.trim())}
    >
      {done ? (
        <a
          href={done.url}
          target="_blank"
          rel="noreferrer"
          data-testid="pr-link"
          className="flex items-center gap-2 min-h-11 font-mono text-sm break-all"
          style={{ color: C.textPrimary }}
        >
          <ExternalLink size={14} className="shrink-0" style={{ color: C.textMuted }} aria-hidden="true" />
          {done.url.replace(/^https:\/\//, "")}
        </a>
      ) : draft.isLoading ? (
        <Loader2 size={16} className="animate-spin" style={{ color: C.textMuted }} aria-label={t("act.loading")} />
      ) : draft.isError ? (
        <ErrorLine error={draft.error} />
      ) : (
        d && (
          <>
            <p>
              {t.rich("act.prBody", {
                branch: d.branch,
                base: d.base,
                mono: (chunks) => (
                  <span className="font-mono" style={{ color: C.textPrimary }}>
                    {chunks}
                  </span>
                ),
              })}
            </p>
            <label className="flex flex-col gap-2">
              <span className="text-xs" style={{ color: C.textMuted }}>
                {t("act.prTitleLabel")}
              </span>
              <input
                data-testid="pr-title"
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                className="w-full px-3 py-2 min-h-11 rounded-md text-base md:text-sm outline-none"
                style={{ background: C.bgDeep, color: C.textPrimary, border: `1px solid ${C.border}` }}
              />
            </label>
            {d.uncommitted > 0 && (
              <p className="text-xs" style={{ color: C.warning }}>
                {t("act.prUncommitted", { count: d.uncommitted })}
              </p>
            )}
          </>
        )
      )}
      {mutation.isError && <ErrorLine error={mutation.error} />}
    </ActionSheet>
  );
}
