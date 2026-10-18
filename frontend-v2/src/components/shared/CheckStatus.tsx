"use client";

// <CheckStatus> — the one way MC shows "was this setting tested, and when?"
// (ADR-093 §2). It reads an item from the check registry (lib/configChecks)
// and says, in one line: works / failed / never tested, plus the age of the
// result ("tested 3 min ago"). A thing without a test procedure says so and
// is never shown green.
//
// Colour carries the state once (the dot); words stay neutral so a failure
// is not signalled twice (DESIGN K6). The reason of a failure follows on its
// own line, worded from the backend's reason code in the UI language
// (<CheckReason>, lib/checkReason). The server's English free text appears
// only for an unknown reason, folded away as a detail.
import { useEffect, useState, type CSSProperties } from "react";
import { useTranslations } from "next-intl";
import { Loader2 } from "lucide-react";
import type { ConfigCheckItem } from "@/lib/types";
import { ageBucket, currentAgeSeconds } from "@/lib/configChecks";
import { checkReasonMessage } from "@/lib/checkReason";
import { StatusDot } from "@/components/shared/StatusDot";

type Phase = "running" | "ok" | "fail" | "never" | "changed" | "notTestable" | "notSet";

export function checkPhase(item: ConfigCheckItem | undefined, running = false): Phase {
  if (running) return "running";
  if (!item) return "never";
  if (!item.configured) return "notSet";
  if (!item.testable) return "notTestable";
  if (item.state === "ok") return "ok";
  if (item.state === "fail") return "fail";
  return item.value_changed ? "changed" : "never";
}

/** Re-render every 30 s so "tested just now" turns into "1 min ago". */
function useNow(intervalMs = 30_000) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs]);
  return now;
}

export function CheckAge({ item }: { item: ConfigCheckItem }) {
  const t = useTranslations("checks");
  const now = useNow();
  const age = currentAgeSeconds(item, now);
  if (age == null) return null;
  const { key, n } = ageBucket(age);
  return <span data-testid="check-age">{t(`age.${key}`, { n })}</span>;
}

/** The reason of a result in the UI language. For `unknown`, the server's
 *  free text (English, scrubbed) sits in a fold below — never in the line. */
export function CheckReason({
  item,
  detail: detailText,
  testId = "check-message",
  className,
  style,
}: {
  item: ConfigCheckItem | undefined | null;
  /** Free text for the fold of an unknown reason (default: item.message) —
   *  e.g. the older test button's own answer. */
  detail?: string | null;
  testId?: string;
  className?: string;
  style?: CSSProperties;
}) {
  const t = useTranslations("checks");
  const reason = checkReasonMessage(item);
  if (!reason) return null;
  const detail = reason.code === "unknown" ? (detailText ?? item?.message) : null;
  return (
    <div data-testid={testId} data-reason={reason.code} className={className} style={style}>
      <span>{t(`reason.${reason.key}`, reason.values)}</span>
      {detail && (
        <details className="mt-1" data-testid="check-detail">
          <summary className="cursor-pointer" style={{ color: "var(--color-text-muted)" }}>
            {t("details")}
          </summary>
          <span className="block mt-1 font-mono break-words" style={{ color: "var(--color-text-muted)" }}>
            {detail}
          </span>
        </details>
      )}
    </div>
  );
}

export function CheckStatus({
  item,
  running = false,
  onTest,
  variant = "full",
  className,
}: {
  item: ConfigCheckItem | undefined;
  running?: boolean;
  /** Shows a "Test" text button (runs the check now, decision E8a). */
  onTest?: () => void;
  /** "age": only the age ("tested 3 min ago" / "never tested") — for cards
   *  that already show the state in a richer headline. */
  variant?: "full" | "age";
  className?: string;
}) {
  const t = useTranslations("checks");
  const phase = checkPhase(item, running);
  const hasAge = (phase === "ok" || phase === "fail") && item?.checked_at;
  const canTest = !!onTest && !!item && item.configured && item.testable && !running;
  const testButton = canTest ? (
    <button
      type="button"
      onClick={onTest}
      title={item?.cost === "quota" ? t("testQuotaHint") : undefined}
      data-testid="check-test"
      className="min-h-11 px-2 text-xs font-medium cursor-pointer"
      style={{ color: "var(--color-text-secondary)" }}
    >
      {t("test")}
    </button>
  ) : null;

  if (variant === "age") {
    // Nothing known yet (list still loading) or nothing set: say nothing
    // rather than a "never tested" that may be wrong a moment later.
    if (!running && (!item || !item.configured)) return null;
    return (
      <div
        data-testid="check-status"
        data-state={phase}
        className={`flex items-center gap-2 text-xs ${className ?? ""}`}
        style={{ color: "var(--color-text-muted)" }}
      >
        {phase === "running" ? (
          <span>{t("running")}</span>
        ) : hasAge && item ? (
          <CheckAge item={item} />
        ) : phase === "notTestable" ? (
          <span>{t("state.notTestable")}</span>
        ) : phase === "notSet" ? null : (
          <span>{t(phase === "changed" ? "state.changed" : "state.never")}</span>
        )}
        {testButton}
      </div>
    );
  }

  const dot: "online" | "error" | "idle" =
    phase === "ok" ? "online" : phase === "fail" ? "error" : "idle";

  return (
    <div data-testid="check-status" data-state={phase} className={className}>
      <div className="flex items-center gap-2 text-xs" role="status">
        {phase === "running" ? (
          <Loader2 size={12} className="animate-spin" style={{ color: "var(--color-text-muted)" }} />
        ) : (
          <StatusDot status={dot} size="sm" />
        )}
        <span style={{ color: "var(--color-text-secondary)" }}>
          {phase === "running" ? t("running") : t(`state.${phase}`)}
          {hasAge && item && (
            <span style={{ color: "var(--color-text-muted)" }}>
              {" · "}
              <CheckAge item={item} />
            </span>
          )}
        </span>
        {testButton}
      </div>
      {phase === "fail" && (
        <CheckReason
          item={item}
          className="text-xs mt-1"
          style={{ color: "var(--color-text-secondary)", maxWidth: "72ch" }}
        />
      )}
    </div>
  );
}
