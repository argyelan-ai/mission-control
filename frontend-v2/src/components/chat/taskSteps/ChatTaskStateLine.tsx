"use client";

/**
 * "● Task in progress · for 1 h" — a task's state in its chat section
 * (spec A §2.5). One sentence from `deriveChatTaskLine`, i.e. the same words
 * and time forms as the task detail. The dot is the section head's one status
 * colour (K6); it pulses while the task runs. Re-renders once a minute on its
 * own — the section around it does not.
 */
import { memo } from "react";
import { useLocale, useTranslations } from "next-intl";
import { C, STATUS_TEXT } from "@/lib/colors";
import type { ChatTaskStub } from "@/lib/chatTypes";
import { capitalize, deriveChatTaskLine, type StateTone } from "@/lib/taskDetail/stateLine";
import { useNow } from "@/hooks/useNow";

export const TONE_DOT: Record<StateTone, string> = {
  error: STATUS_TEXT.error,
  info: C.info,
  online: C.online,
  warning: C.warning,
  accent: C.accent,
  muted: C.textMuted,
};

export const ChatTaskStateLine = memo(function ChatTaskStateLine({ stub }: { stub: ChatTaskStub }) {
  const t = useTranslations();
  const locale = useLocale();
  const now = useNow(60_000);
  const line = deriveChatTaskLine(stub, locale, new Date(now));
  const word = capitalize(t(`${line.word.ns}.${line.word.key}`), locale);
  const detail = line.detail ? t(`tasks.detail.line.${line.detail.key}`, line.detail.values) : null;
  const pulse = stub.status === "in_progress" && stub.live;
  return (
    <p
      data-testid="chat-task-state"
      data-tone={line.tone}
      className="m-0 px-4 min-h-8 flex items-center gap-2 text-xs"
      style={{ color: C.textMuted }}
    >
      <span className="w-4 shrink-0 flex justify-center" aria-hidden="true">
        <span
          className={`inline-block w-2 h-2 rounded-full ${pulse ? "animate-pulse motion-reduce:animate-none" : ""}`}
          style={{ background: TONE_DOT[line.tone] }}
        />
      </span>
      <span className="min-w-0 truncate">
        {word}
        {detail && ` · ${detail}`}
      </span>
    </p>
  );
});
