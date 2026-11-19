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
 *
 * S1-Rest (bauplan §9): some notices carry only a code — "new chat started"
 * after a recreate, "storage not moved" when the move of old chat copies did
 * not run. Their sentence has no placeholder, so it is translated without
 * values; before, the card showed the daemon's English text under a German
 * label (K10).
 *
 * Incident 21.10.2026: a chat reset left the operator an empty chat with no
 * word why. Every conversation the daemon opens in place of another one now
 * starts with `new_conversation` ("New conversation started · 17:07 · the
 * previous one is saved"); the recreate card `chat_started_fresh` is the
 * same thing for the same operator and carries the same label and the time.
 * The time is the line's own (`ev.ts`), in the app's one clock format
 * (lib/clockTime) — the day too when it was not today. `notice.previous`/
 * `current` hold the two session ids: data for the "show previous" link of
 * P4 (`action`), never shown as text. `notice.kept: false` (the old copy is
 * not on disk): the card still says a new conversation began, but makes no
 * promise — no sentence.
 */
import type { ReactNode } from "react";
import { Info } from "lucide-react";
import { useLocale, useTranslations } from "next-intl";

import { formatClockTime } from "@/lib/clockTime";
import { C, STATUS_TEXT } from "@/lib/colors";
import type { MessageEvent } from "@/lib/chatTypes";
import { TIMELINE_CARD_GUTTER } from "./timelineCard";

interface ChatNoticeCardProps {
  ev: MessageEvent;
  /** Below the sentence: the place for the later "show previous" link
   *  (P4, session list/resume). Nothing until then. */
  action?: ReactNode;
}

/** Notices that mark the start of a new conversation: they show when. */
export const CONVERSATION_START_CODES: ReadonlySet<string> = new Set([
  "new_conversation",
  "chat_started_fresh",
]);

/** The values a notice sentence needs, per code. A code that is not listed
 *  has a sentence without placeholders. Written down once so a sentence is
 *  only filled when ALL its values arrived — a raw `{previous}` never reaches
 *  the screen (ChatNoticeCard.test.tsx checks this list against the
 *  catalogs). */
export const NOTICE_VALUES: Record<string, readonly ("previous" | "current")[]> = {
  model_fallback: ["previous", "current"],
};

export function ChatNoticeCard({ ev, action }: ChatNoticeCardProps) {
  const t = useTranslations("chat");
  const locale = useLocale();
  const code = ev.notice?.code ?? ev.source?.title ?? "";
  const previous = ev.notice?.previous ?? "";
  const current = ev.notice?.current ?? "";
  const sentenceKey = `notice.${code}`;
  const labelKey = `notice.${code}Label`;
  // A code we have a sentence for, with every value it needs: the localized
  // line. Anything else falls back to the daemon's own (English) text — a
  // notice is never swallowed.
  const values = { previous, current };
  const needed = NOTICE_VALUES[code] ?? [];
  // A new conversation whose predecessor's copy is not on disk: the label
  // and the time say what happened; "the previous one is saved" would be
  // false (critic finding, incident 21.10.2026).
  const promiseBroken = code === "new_conversation" && ev.notice?.kept === false;
  const sentence = promiseBroken
    ? null
    : code && t.has(sentenceKey) && needed.every((name) => values[name])
      ? needed.length > 0
        ? t(sentenceKey, values)
        : t(sentenceKey)
      : ev.text;
  const label = code && t.has(labelKey) ? t(labelKey) : t("notice.label");
  const clock = CONVERSATION_START_CODES.has(code) ? formatClockTime(ev.ts, locale) : null;

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
          {clock && (
            <time
              data-testid="chat-notice-time"
              dateTime={ev.ts}
              className="text-xs tabular-nums"
              style={{ color: C.textMuted }}
            >
              {clock}
            </time>
          )}
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
        {action && (
          <div data-testid="chat-notice-action" className="mt-2">
            {action}
          </div>
        )}
      </div>
    </div>
  );
}
