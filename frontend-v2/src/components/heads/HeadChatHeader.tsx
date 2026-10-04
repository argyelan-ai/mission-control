"use client";

/**
 * HeadChatHeader — the head chat's own, deliberately small header (bauplan
 * `heads-sichtbar` PR 2 §3.2: "Kopfzeile = Titel + Kontextzeile"). No
 * live/beendet badge (a head's aliveness is already the dot in the Chats
 * list — repeating it here would be the one fact twice, K3), no detail-
 * level or Chat/Terminal toggle (a head has no terminal), no microphone (no
 * composer to dictate into). Kept as its OWN component rather than a branch
 * inside `ChatView`'s existing header JSX: that header is tightly woven
 * around agent-only fields (`agent.headless_chat`, badge width math for the
 * live/ended pill, …) and `ChatView` itself is frozen — a `head` branch
 * here touches zero of those lines.
 */
import { useLocale, useTranslations } from "next-intl";
import { ChevronLeft } from "lucide-react";
import { C } from "@/lib/colors";
import { formatAge, formatDuration } from "@/lib/taskDetail/format";
import { headListTitle, headStateKey, pairShort, runDurationSeconds, type HeadRun } from "@/lib/heads";

/** Same visual weight as `ChatView`'s own back chevron (36px circle, 44px
 *  touch target via `-m-1`) — not re-exported from there (that module is
 *  frozen) because the two markups are simple enough that duplicating the
 *  handful of classes costs less than threading an import across a module
 *  boundary just for a button. */
const TOUCH_TARGET = "min-w-touch min-h-touch -m-1";

export function HeadChatHeader({ head, onBack }: { head: HeadRun; onBack?: () => void }) {
  const t = useTranslations("heads");
  const locale = useLocale();

  const pair = pairShort(head);
  let contextLine: string;
  if (head.state === "needs_you") {
    contextLine = t(headStateKey(head.state));
  } else if (head.state === "starting") {
    contextLine = `${pair} · ${t("time.startingNow")}`;
  } else if (head.state === "running") {
    const seconds = runDurationSeconds(head);
    const duration = seconds != null ? formatDuration(seconds, locale) : null;
    contextLine = duration ? `${pair} · ${t("time.runningFor", { duration })}` : pair;
  } else {
    const age = formatAge(head.exited_at ?? head.created_at, locale);
    contextLine = age ? `${t(headStateKey(head.state))} · ${t("time.ago", { age })}` : t(headStateKey(head.state));
  }

  return (
    <div
      data-testid="chat-header"
      data-kind="head"
      className="relative flex items-center gap-2 pl-1 pr-2 md:px-4 py-2 md:py-3 pt-safe-top md:pt-3 border-b shrink-0 bg-[var(--color-bg-surface)]"
      style={{ borderColor: C.border }}
    >
      {onBack && (
        <button
          type="button"
          onClick={onBack}
          aria-label={t("chat.backToChats")}
          data-testid="head-chat-back"
          className={`relative z-10 flex md:hidden items-center justify-center shrink-0 cursor-pointer ${TOUCH_TARGET}`}
        >
          <span
            className="flex items-center justify-center w-9 h-9 rounded-full"
            style={{ color: C.textSecondary, backgroundColor: C.bgHover }}
          >
            <ChevronLeft size={19} />
          </span>
        </button>
      )}
      {/* No absolute-centered title on the phone the way the agent header
       *  does it (that trick exists only to dodge a live/ended badge on the
       *  right, which a head header never has) — a left-flowed title with
       *  room to breathe is simpler and correct here on every breakpoint. */}
      <div className="flex flex-col min-w-0 flex-1">
        <span className="text-sm font-semibold md:font-medium truncate" style={{ color: C.textPrimary }}>
          {headListTitle(head)}
        </span>
        <span className="text-xs truncate leading-tight" style={{ color: C.textMuted }}>
          {contextLine}
        </span>
      </div>
    </div>
  );
}
