"use client";

/**
 * ChatNoticeCard — something Mission Control decided for the operator,
 * shown where it happened in the timeline.
 *
 * Not an error: the turn goes on. But the operator must see it, because it
 * changes what runs. First case (incident 19.10.2026): after a runtime
 * switch the chat session was still pinned to the old model, which the
 * runtime no longer offers. The daemon now pins the runtime's current model
 * instead and says so here — "Previously chosen model … is not available
 * anymore — using …".
 *
 * Signal doctrine (DESIGN.md): colour means status. A notice is the warning
 * tone, on the frame and the label chip only; the sentence stays in calm
 * secondary text (K6: one status colour per area, never doubled).
 */
import { Info } from "lucide-react";
import { useTranslations } from "next-intl";

import { C, STATUS_TEXT } from "@/lib/colors";
import type { MessageEvent } from "@/lib/chatTypes";
import { TIMELINE_CARD_GUTTER } from "./timelineCard";

interface ChatNoticeCardProps {
  ev: MessageEvent;
}

export function ChatNoticeCard({ ev }: ChatNoticeCardProps) {
  const t = useTranslations("chat");
  const code = ev.notice?.code ?? ev.source?.title ?? "";
  const previous = ev.notice?.previous ?? "";
  const current = ev.notice?.current ?? "";
  const sentenceKey = `notice.${code}`;
  const labelKey = `notice.${code}Label`;
  // A code we have a sentence for, with its values: the localized line.
  // Anything else falls back to the daemon's own (English) text — a notice
  // is never swallowed.
  const sentence =
    code && previous && current && t.has(sentenceKey)
      ? t(sentenceKey, { previous, current })
      : ev.text;
  const label = code && t.has(labelKey) ? t(labelKey) : t("notice.label");

  return (
    <div className={TIMELINE_CARD_GUTTER}>
      <div
        data-testid="chat-notice-card"
        data-code={code || undefined}
        role="status"
        className="rounded-lg px-3 py-2"
        style={{ border: `1px solid ${C.warning}`, background: C.bgElevated }}
      >
        <div className="flex items-center gap-2">
          <Info size={14} style={{ color: STATUS_TEXT.warning }} aria-hidden="true" />
          <span
            data-testid="chat-notice-label"
            className="rounded px-2 py-1 text-xs font-medium leading-none"
            style={{ color: STATUS_TEXT.warning, border: `1px solid ${C.warning}` }}
          >
            {label}
          </span>
        </div>
        {sentence && (
          <p
            data-testid="chat-notice-text"
            className="mt-2 break-words whitespace-pre-wrap text-sm leading-normal"
            style={{ color: C.textSecondary }}
          >
            {sentence}
          </p>
        )}
      </div>
    </div>
  );
}
