"use client";

import { memo, useEffect, useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import { Brain, ChevronRight } from "lucide-react";
import { C } from "@/lib/colors";
import type { PreviewEvent } from "@/lib/chatTypes";
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
  const elapsed = Number.isNaN(startedMs) ? null : formatElapsed(now - startedMs);

  return (
    <div className="w-full px-4" data-testid="live-thinking" aria-live="polite">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="w-full min-h-11 flex items-center gap-2 text-left bg-transparent border-0 p-0 cursor-pointer text-xs"
      >
        <Brain size={14} className="shrink-0 animate-pulse motion-reduce:animate-none" style={{ color: C.textMuted }} aria-hidden="true" />
        <span className="min-w-0 truncate">
          <span style={{ color: C.textSecondary }}>{t("thinkingLive")}</span>
          {tokens && <span className="tabular-nums" style={{ color: C.textMuted }}> · {tokens}</span>}
          {elapsed && <span className="tabular-nums" style={{ color: C.textMuted }}> · {elapsed}</span>}
        </span>
        <ChevronRight
          size={14}
          className={`shrink-0 transition-transform motion-reduce:transition-none ${open ? "rotate-90" : ""}`}
          style={{ color: C.textMuted }}
          aria-hidden="true"
        />
      </button>
      {open && (
        <div
          data-testid="live-thinking-text"
          className="ml-6 pb-2 flex flex-col justify-end overflow-hidden max-h-[calc(8*1.6*12px)]"
        >
          <p className="m-0 text-xs italic leading-relaxed whitespace-pre-wrap break-words max-w-[76ch]" style={{ color: C.textMuted }}>
            {preview.text}
          </p>
        </div>
      )}
    </div>
  );
});
