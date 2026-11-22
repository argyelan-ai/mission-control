"use client";

import { useContext, useEffect, useState } from "react";
import { QueryClientContext } from "@tanstack/react-query";
import { useLocale, useTranslations } from "next-intl";
import { isSwitching, useChatSwitch } from "@/hooks/useChatSwitch";
import { TaskStepsAgentContext } from "@/hooks/useTaskStepDetail";
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

/** One predicate for "a turn runs" — the subline's "Working" and the speed
 *  line's live tok/s agree on it (shell review A5). */
export function isTurnRunning(state: StateEvent | null, sending = false): boolean {
  return sublineKind(state, "active", sending) === "working";
}

/**
 * The one muted line under the chat title (variant C): "Working for 3 min",
 * "Ready". The model is named once, in the composer's chip (operator
 * pictures 18.11.2026 and user test 21.11.2026: the same id stood twice). The duration counts from the operator's last
 * message in the transcript — the start of the running turn — and only
 * shows from one minute on (K17). Nothing known → no line (K3).
 */
type SublineProps = {
  state: StateEvent | null;
  aliveness: ChatAliveness;
  turnStartedAt: string | null;
  sending?: boolean;
  /** A restart or switch back (ADR-105) is open: the line says so instead
   *  of the state word — the card in the timeline names the pair (K3). A
   *  model-only switch does not: the session keeps answering meanwhile. */
  switching?: boolean;
};

export function ChatHeaderSubline(props: SublineProps) {
  // Outside a query client (a header rendered on its own) there is no
  // record to ask; inside, an open switch is read from it.
  if (!useContext(QueryClientContext)) return <SublineView {...props} />;
  return <SublineWithSwitch {...props} />;
}

function SublineWithSwitch(props: SublineProps) {
  const sw = useChatSwitch(useContext(TaskStepsAgentContext));
  return <SublineView {...props} switching={sw.enabled && isSwitching(sw.switches)} />;
}

export function SublineView({
  state,
  aliveness,
  turnStartedAt,
  sending = false,
  switching = false,
}: SublineProps) {
  const t = useTranslations("cockpit.header");
  const ts = useTranslations("cockpit.switch");
  const locale = useLocale();
  const now = useNow(30_000);
  const kind = sublineKind(state, aliveness, sending);
  // When the operator last sent, locally. The duration never starts before
  // it: right after a send the transcript still ends with the PREVIOUS
  // operator message, and the header read "Working for 23 h" (user test
  // 21.11.2026). Kept until the turn ends, so a send flag that clears
  // before the new message reaches the transcript changes nothing.
  const [sentAt, setSentAt] = useState<number | null>(null);
  useEffect(() => {
    if (sending) setSentAt((prev) => prev ?? Date.now());
  }, [sending]);
  useEffect(() => {
    if (kind !== "working") setSentAt(null);
  }, [kind]);
  if (switching && aliveness !== "ended") {
    return (
      <span data-testid="chat-header-subline" data-kind="switching" className="text-xs leading-tight truncate" style={{ color: C.textMuted }}>
        {ts("switching")}
      </span>
    );
  }
  if (!kind) return null;

  let text: string | null;
  if (kind === "working") {
    const floor = sentAt ?? (sending ? now : null);
    let started = turnStartedAt ? Date.parse(turnStartedAt) : NaN;
    if (floor != null && (Number.isNaN(started) || started < floor)) started = floor;
    const seconds = Number.isNaN(started) ? null : Math.max(0, now - started) / 1000;
    const span = seconds != null && seconds >= 60 ? formatSpan(seconds, locale) : null;
    text = span ? t("workingFor", { span }) : t("working");
  } else if (kind === "waiting") {
    text = t("waitingInput");
  } else if (kind === "ready") {
    text = t("ready");
  } else {
    // "ended": the header's own "ended" badge already says it (K3).
    text = null;
  }
  if (!text) return null;

  return (
    <span data-testid="chat-header-subline" data-kind={kind ?? undefined} className="text-xs leading-tight truncate" style={{ color: C.textMuted }}>
      {text}
    </span>
  );
}
