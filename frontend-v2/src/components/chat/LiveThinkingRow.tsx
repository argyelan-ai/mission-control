"use client";

import { memo, useEffect, useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import { Brain, ChevronRight } from "lucide-react";
import { C } from "@/lib/colors";
import type { PreviewEvent } from "@/lib/chatTypes";
import { formatSpan } from "@/lib/taskDetail/stateLine";
import { estimateTokens, formatElapsed } from "./thinkingFormat";

/**
 * The model is thinking RIGHT NOW (operator finding 20.10.2026: minutes of
 * reasoning looked like "nothing happens — no harness does that").
 *
 * Collapsed by default, like Claude Code / Codex: one quiet line "Thinking ·
 * ≈5,900 tokens · 1:45" with a pulsing icon, the clock counting up every
 * second from the thought's start. A tap opens the streamed END of the
 * thought (the bridge only sends its last ~2 000 characters), clamped like
 * the answer preview so the view does not jump. The finished thought later
 * stands in the history as a `ThinkingRow`.
 *
 * Nothing is invented: no start -> no clock, no size -> no token estimate.
 */
/** No new piece of the thought for this long = the row stops counting. */
export const QUIET_AFTER_MS = 60_000;

export const LiveThinkingRow = memo(function LiveThinkingRow({ preview }: { preview: PreviewEvent }) {
  const t = useTranslations("sessions");
  const locale = useLocale();
  const [open, setOpen] = useState(false);
  const startedMs = preview.startedAt ? Date.parse(preview.startedAt) : NaN;
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (Number.isNaN(startedMs)) return;
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [startedMs]);

  const tokens = typeof preview.chars === "number" && preview.chars > 0
    ? t("thinkingTokens", { count: new Intl.NumberFormat(locale).format(estimateTokens(preview.chars)) })
    : null;
  // A dead engine sends nothing more: the clock must not keep counting as if
  // the model were still at work (review finding). Past QUIET_AFTER_MS without
  // a new snapshot the clock stops at the last one and the row says how long
  // it has been quiet. Whether the model is reachable is the status line's job.
  const lastMs = Date.parse(preview.ts);
  const quietMs = Number.isNaN(lastMs) ? 0 : now - lastMs;
  const quiet = quietMs > QUIET_AFTER_MS;
  const elapsed = Number.isNaN(startedMs)
    ? null
    : formatElapsed((quiet ? lastMs : now) - startedMs);
  // K10: the repo's one form for "how long silent" — a span ("silent for
  // 2 min" / "still seit 2 min"), never a second m:ss clock (review UI-6).
  const quietText = quiet ? t("thinkingQuiet", { span: formatSpan(quietMs / 1000, locale) ?? "" }) : null;

  // No live region around the ticking clock (review R4/UI-3: a screen reader
  // read "1:47", "1:48", … every second). One calm status says only
  // "Thinking" or how long it has been silent — it changes at most once a
  // minute.
  return (
    <div className="w-full px-4" data-testid="live-thinking">
      <span role="status" className="sr-only">
        {quietText ? `${t("thinkingLive")} · ${quietText}` : t("thinkingLive")}
      </span>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="w-full min-h-11 flex items-center gap-2 text-left bg-transparent border-0 p-0 cursor-pointer text-xs"
      >
        {/* 13 px + gap-2 like ToolRow / ThinkingRow: one text edge (review UI-7). */}
        <Brain size={13} className={`shrink-0 ${quiet ? "" : "animate-pulse motion-reduce:animate-none"}`} style={{ color: C.textMuted }} aria-hidden="true" />
        <span className="min-w-0 truncate">
          <span style={{ color: C.textSecondary }}>{t("thinkingLive")}</span>
          {tokens && <span className="tabular-nums" style={{ color: C.textMuted }}> · {tokens}</span>}
          {elapsed && <span className="tabular-nums" style={{ color: C.textMuted }}> · {elapsed}</span>}
          {quietText && (
            <span className="tabular-nums" style={{ color: C.textMuted }}>
              {" · "}{quietText}
            </span>
          )}
        </span>
        <ChevronRight
          size={14}
          className={`shrink-0 transition-transform motion-reduce:transition-none ${open ? "rotate-90" : ""}`}
          style={{ color: C.textMuted }}
          aria-hidden="true"
        />
      </button>
      {open && (
        <div data-testid="live-thinking-text" className="flex gap-2 pb-2">
          {/* Spacer in icon width: the text starts on the label's edge. */}
          <span data-indent="" className="w-[13px] shrink-0" aria-hidden="true" />
          <div className="min-w-0 flex flex-col justify-end overflow-hidden max-h-[calc(8*1.6*12px)]">
            <p className="m-0 text-xs italic leading-relaxed whitespace-pre-wrap break-words max-w-[76ch]" style={{ color: C.textMuted }}>
              {preview.text}
            </p>
          </div>
        </div>
      )}
    </div>
  );
});
