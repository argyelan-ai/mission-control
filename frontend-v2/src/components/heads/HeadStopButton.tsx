"use client";

/**
 * Stop with an inline confirmation (review: a 30-minute run must not end on a
 * mistap on the phone). Same pattern as the runtime stop on /runtimes: the
 * first click only asks, the second one stops.
 *
 *   [■ Stop]   →   Stop the head? The branch stays.  [Stop] [Cancel]
 */

import { useState } from "react";
import { useTranslations } from "next-intl";
import { Square } from "lucide-react";
import { C, STATUS_TEXT } from "@/lib/colors";

const btn =
  "inline-flex items-center justify-center gap-1.5 px-3 min-h-[36px] pointer-coarse:min-h-[44px] rounded-md text-xs font-medium cursor-pointer transition-colors hover:bg-[var(--color-bg-hover)] disabled:opacity-40 disabled:cursor-not-allowed";

export function HeadStopButton({
  onStop,
  pending = false,
  done = false,
  label,
  testId = "head-stop",
}: {
  onStop: () => void;
  pending?: boolean;
  done?: boolean;
  /** Text of the first button (default: heads.card.stop). */
  label?: string;
  testId?: string;
}) {
  const t = useTranslations("heads.card");
  const [confirming, setConfirming] = useState(false);

  if (confirming && !pending && !done) {
    return (
      <span className="inline-flex flex-wrap items-center gap-2" role="group" data-testid={`${testId}-confirm`}>
        <span className="text-xs" style={{ color: C.textPrimary }}>{t("stopConfirm")}</span>
        <button
          type="button"
          autoFocus
          onClick={() => {
            setConfirming(false);
            onStop();
          }}
          data-testid={`${testId}-confirm-yes`}
          className={btn}
          style={{ color: STATUS_TEXT.error, border: `1px solid ${C.error}66` }}
        >
          <Square size={12} aria-hidden />
          {t("stopConfirmYes")}
        </button>
        <button
          type="button"
          onClick={() => setConfirming(false)}
          data-testid={`${testId}-confirm-no`}
          className={btn}
          style={{ color: C.textSecondary, border: `1px solid ${C.borderActive}` }}
        >
          {t("stopCancel")}
        </button>
      </span>
    );
  }

  return (
    <button
      type="button"
      onClick={() => setConfirming(true)}
      disabled={pending || done}
      data-testid={testId}
      className={btn}
      style={{ color: STATUS_TEXT.error, border: `1px solid ${C.borderActive}` }}
    >
      <Square size={12} aria-hidden />
      {pending ? t("stopping") : done ? t("stopRequested") : label ?? t("stop")}
    </button>
  );
}
