"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useLocale, useTranslations } from "next-intl";
import { CircleAlert, ExternalLink, Loader2 } from "lucide-react";
import { C, STATUS_TEXT, alpha } from "@/lib/colors";
import { timeAgo } from "@/lib/utils";
import type { BenchEntryTaskState } from "./types";

// A bench brief is a one-shot index.html. Ten minutes without a single
// server-observed sign of life (agent comment, harvested model turn, work
// start) is worth telling the operator about — stated as a fact with a link,
// not as a verdict: the task may still be thinking through one long turn.
export const SILENT_AFTER_MS = 10 * 60_000;

function taskHref(taskId: string) {
  return `/tasks?task=${encodeURIComponent(taskId)}`;
}

function useNow(intervalMs = 30_000) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs]);
  return now;
}

/**
 * Placeholder body of a running agent entry card: says WHY the entry waits
 * (blocked on a question, or no sign of life) instead of a bare spinner
 * (fix-vorschlag 19.10. point 7). The entry itself stays "generating" — a
 * blocked task is temporary and its later delivery is still collected.
 */
export function EntryTaskState({
  state,
  generatingLabel,
}: {
  state: BenchEntryTaskState | null | undefined;
  generatingLabel: string;
}) {
  const t = useTranslations("bench.challengeDetail");
  const locale = useLocale();
  const now = useNow();

  if (state?.task_status === "blocked") {
    return (
      <div
        className="flex flex-col gap-2 rounded-lg p-3"
        style={{
          backgroundColor: alpha(C.warning, 0.08),
          border: `1px solid ${alpha(C.warning, 0.3)}`,
        }}
      >
        <div className="flex items-center gap-2">
          <CircleAlert size={14} style={{ color: STATUS_TEXT.warning }} aria-hidden />
          <span className="text-xs font-medium" style={{ color: STATUS_TEXT.warning }}>
            {t("entryBlocked")}
          </span>
        </div>
        {state.blocker_reason && (
          <p
            className="text-xs line-clamp-4 break-words"
            style={{ color: C.textPrimary }}
            title={state.blocker_reason}
          >
            {state.blocker_reason}
          </p>
        )}
        <p className="text-xs" style={{ color: C.textMuted }}>
          {state.blocked_at && <>{t("entryBlockedSince", { ago: timeAgo(state.blocked_at, locale) })} · </>}
          {t("entryBlockedHint")}
        </p>
        <TaskLink taskId={state.task_id} label={t("openTask")} align="self-start" />
      </div>
    );
  }

  const lastMs = state?.last_activity_at ? new Date(state.last_activity_at).getTime() : null;
  const silent = lastMs !== null && now - lastMs > SILENT_AFTER_MS;

  return (
    <div
      className="flex flex-col items-center justify-center gap-2 py-8 rounded-lg px-3"
      style={{ backgroundColor: C.bgDeep, color: C.textMuted }}
    >
      <div className="flex items-center gap-2">
        <Loader2 size={15} className="animate-spin" />
        <span className="text-xs">{generatingLabel}</span>
      </div>
      {silent && state && (
        <>
          <span className="text-xs text-center" style={{ color: STATUS_TEXT.warning }}>
            {t("entrySilent", { ago: timeAgo(state.last_activity_at, locale) })}
          </span>
          <TaskLink taskId={state.task_id} label={t("openTask")} align="self-center" />
        </>
      )}
    </div>
  );
}

function TaskLink({
  taskId,
  label,
  align,
}: {
  taskId: string;
  label: string;
  align: "self-start" | "self-center";
}) {
  return (
    <Link
      href={taskHref(taskId)}
      className={`flex items-center gap-1 text-xs ${align}`}
      style={{ color: C.accent }}
    >
      <ExternalLink size={12} aria-hidden /> {label}
    </Link>
  );
}
