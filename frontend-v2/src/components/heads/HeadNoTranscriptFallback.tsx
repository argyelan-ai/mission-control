"use client";

/**
 * HeadNoTranscriptFallback — what the head chat shows instead of an empty
 * screen when there is no transcript to render (bauplan `heads-sichtbar`
 * PR 2 §7: "Heads ohne Leser (kimi, grok): `source:"none"` → Chat zeigt
 * Schritt + `head.log`-Ende (bestehender `/log`) statt leer"). Covers every
 * `reason` the backend's `services/heads/transcript.py` can return
 * (`no_reader`, `not_yet`, `no_transcript`, `too_large`) with ONE fallback:
 * the existing, already-masked `/log` tail — a harness with no reader at
 * all is told so explicitly; every other reason (just started, ended
 * before writing anything, file too big to read) gets the plainer "no
 * output yet" heading, since there IS a reader, it is just a transient or
 * edge state.
 */
import { useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { api } from "@/lib/api";
import { C } from "@/lib/colors";

export function HeadNoTranscriptFallback({
  runId,
  reason,
}: {
  runId: string;
  reason: string | null;
}) {
  const t = useTranslations("heads");
  const logQuery = useQuery({
    queryKey: ["heads", runId, "log"],
    queryFn: () => api.heads.log(runId, 200),
    retry: false,
  });

  const noReader = reason === "no_reader";

  return (
    <div className="flex flex-col gap-2 px-4 py-6" data-testid="head-no-transcript">
      <p className="text-sm font-medium" style={{ color: C.textSecondary }}>
        {noReader ? t("chat.noTranscriptTitle") : t("chat.logEmpty")}
      </p>
      {noReader && (
        <p className="text-xs" style={{ color: C.textMuted }}>
          {t("chat.noTranscriptHint")}
        </p>
      )}
      <pre
        className="rounded-dense p-3 text-xs leading-snug font-mono overflow-auto max-h-[320px] whitespace-pre-wrap break-words"
        style={{ background: C.bgDeep, color: C.textSecondary, border: `1px solid ${C.border}` }}
        data-testid="head-no-transcript-log"
      >
        {logQuery.data === undefined ? "…" : logQuery.data.trim() ? logQuery.data : t("chat.logEmpty")}
      </pre>
    </div>
  );
}
