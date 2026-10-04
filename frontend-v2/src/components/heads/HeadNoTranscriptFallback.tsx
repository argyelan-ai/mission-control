"use client";

/**
 * HeadNoTranscriptFallback — what the head chat shows instead of an empty
 * screen when there is no transcript to render (bauplan `heads-sichtbar`
 * PR 2 §7: "Heads ohne Leser (kimi, grok): `source:"none"` → Chat zeigt
 * Schritt + `head.log`-Ende (bestehender `/log`) statt leer"). Covers every
 * `reason` the backend's `services/heads/transcript.py` can return
 * (`no_reader`, `not_yet`, `no_transcript`, `too_large`) with its own
 * one-line heading — review finding on PR #756 round 3: every reason other
 * than `no_reader` used to share one "No output yet." heading, which was
 * simply wrong for `no_transcript`/`too_large` (an ENDED run, nothing more
 * is coming — "yet" promises output that will never arrive; 15 real older
 * runs on disk have no transcript at all). The `/log` tail below is the
 * same for every reason (a harness with no reader is also told so
 * explicitly, via its own hint line) — but only ever shown when it
 * genuinely has content: a loading, failed or truly empty fetch now reads
 * as "nothing to show here" rather than a permanent "…" or a second,
 * duplicate copy of the heading above it.
 */
import { useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { api } from "@/lib/api";
import { C } from "@/lib/colors";

function headingKey(reason: string | null): string {
  switch (reason) {
    case "no_reader":
      return "chat.noTranscriptTitle";
    case "no_transcript":
      return "chat.noTranscriptKept";
    case "too_large":
      return "chat.transcriptTooLarge";
    case "not_yet":
    default:
      // `null` (meta not resolved yet) reads the same as `not_yet` — both
      // are "nothing to show YET", the one heading that still earns "yet".
      return "chat.waitingFirstLines";
  }
}

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
  // A failed fetch (`logQuery.isError`) and a still-loading one both leave
  // `data` `undefined` — both correctly fall out of "has content" here,
  // instead of the old code's permanent "…" placeholder for either case.
  const log = logQuery.data?.trim();

  return (
    <div className="flex flex-col gap-2 px-4 py-6" data-testid="head-no-transcript">
      <p className="text-sm font-medium" style={{ color: C.textSecondary }}>
        {t(headingKey(reason))}
      </p>
      {noReader && (
        <p className="text-xs" style={{ color: C.textMuted }}>
          {t("chat.noTranscriptHint")}
        </p>
      )}
      {log && (
        <pre
          className="rounded-dense p-3 text-xs leading-snug font-mono overflow-auto max-h-[320px] whitespace-pre-wrap break-words"
          style={{ background: C.bgDeep, color: C.textSecondary, border: `1px solid ${C.border}` }}
          data-testid="head-no-transcript-log"
        >
          {logQuery.data}
        </pre>
      )}
    </div>
  );
}
