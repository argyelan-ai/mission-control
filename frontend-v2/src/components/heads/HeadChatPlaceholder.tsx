"use client";

/**
 * HeadChatPlaceholder — what `sessions/page.tsx` shows instead of mounting
 * `ChatView` while a head's own record has not resolved yet, or never will
 * (review finding on PR #756).
 *
 * `selectedHeadId` is set before `selectedHeadRun` resolves in two real
 * cases: the Archive sheet's "tap a row" path (an archived run is never in
 * the 7-day recent list the Heads section already holds, so the one-off
 * fallback fetch resolves a tick later) and every `?head=` deep link (the
 * recent list has not loaded on page load yet). Rendering `ChatView` with
 * `head={null}` during that gap used to fall into its generic "pick a
 * session" copy — with no header, no back chevron, chromeless on the phone
 * — and (before `ChatView`'s own hooks-order fix) crash outright once the
 * real run arrived a tick later on the SAME mounted instance.
 *
 * Kept as its own tiny component rather than a branch inside `ChatView`:
 * it needs none of that component's state (stream, timeline, footer) and
 * `ChatView` itself is frozen (ADR-085 Nachtrag) — this never touches it.
 */
import { useTranslations } from "next-intl";
import { ChevronLeft } from "lucide-react";
import { C } from "@/lib/colors";

const TOUCH_TARGET = "min-w-touch min-h-touch -m-1";

export function HeadChatPlaceholder({
  variant,
  onBack,
}: {
  variant: "loading" | "not_found";
  onBack?: () => void;
}) {
  const t = useTranslations("heads");

  return (
    <div className="flex flex-col flex-1 min-h-0 overflow-hidden" data-testid="head-chat-placeholder" data-variant={variant}>
      <div
        data-testid="chat-header"
        data-kind="head-placeholder"
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
      </div>
      <div className="flex flex-1 flex-col items-center justify-center gap-1 px-6 text-center">
        {variant === "loading" ? (
          <p className="text-xs" style={{ color: C.textMuted }} data-testid="head-chat-resolving">
            {t("chat.resolving")}
          </p>
        ) : (
          <>
            <p className="text-sm font-medium" style={{ color: C.textSecondary }} data-testid="head-chat-not-found">
              {t("chat.notFoundTitle")}
            </p>
            <p className="text-xs" style={{ color: C.textMuted }}>
              {t("chat.notFoundHint")}
            </p>
          </>
        )}
      </div>
    </div>
  );
}
