"use client";

import { useLocale, useTranslations } from "next-intl";
import { C } from "@/lib/colors";
import { useNow } from "@/hooks/useNow";
import { formatSpan } from "@/lib/taskDetail/stateLine";
import type { ChatAliveness, StateEvent } from "@/lib/chatTypes";

export type SublineKind = "working" | "waiting" | "ready" | "ended";

/** Which word the subline says — from the same state the status line shows. */
export function sublineKind(
  state: StateEvent | null,
  aliveness: ChatAliveness,
  sending = false,
): SublineKind | null {
  if (aliveness === "ended") return "ended";
  // A message just went out: the turn has started, whatever the last
  // (stale) probe said — the status line says "Sent…" for the same reason.
  if (sending) return "working";
  switch (state?.status) {
    case "working":
    case "task_working":
    case "waiting_model":
      return "working";
    case "waiting_input":
    case "permission_prompt":
      return "waiting";
    case "idle":
      return "ready";
    default:
      return null;
  }
}

/**
 * The one muted line under the chat title (variant C): "model · Working for
 * 3 min", "model · Ready". The duration counts from the operator's last
 * message in the transcript — the start of the running turn — and only
 * shows from one minute on (K17). Nothing known → no line (K3).
 */
export function ChatHeaderSubline({
  model,
  state,
  aliveness,
  turnStartedAt,
  sending = false,
}: {
  model: string | null;
  state: StateEvent | null;
  aliveness: ChatAliveness;
  turnStartedAt: string | null;
  sending?: boolean;
}) {
  const t = useTranslations("cockpit.header");
  const locale = useLocale();
  const now = useNow(30_000);
  const kind = sublineKind(state, aliveness, sending);
  if (!kind && !model) return null;

  let text: string | null;
  if (kind === "working") {
    const started = turnStartedAt ? Date.parse(turnStartedAt) : NaN;
    const seconds = Number.isNaN(started) ? null : (now - started) / 1000;
    const span = seconds != null && seconds >= 60 ? formatSpan(seconds, locale) : null;
    text = span ? t("workingFor", { span }) : t("working");
  } else if (kind === "waiting") {
    text = t("waitingInput");
  } else if (kind === "ready") {
    text = t("ready");
  } else {
    text = null;
  }
  // "ended" adds no word: the header's own "ended" badge already says it (K3).
  // One text node on purpose: "model · state" reads as one line, and the
  // state word never stands alone next to the status line's own word.
  const line = [model, text].filter(Boolean).join(" · ");

  return (
    <span data-testid="chat-header-subline" data-kind={kind ?? undefined} className="text-xs leading-tight truncate" style={{ color: C.textMuted }}>
      {line}
    </span>
  );
}
