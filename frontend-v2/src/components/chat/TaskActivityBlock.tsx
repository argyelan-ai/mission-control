"use client";

/**
 * "From the task" — what the agent's RUNNING task does, live, inside the
 * agent chat (operator finding 20.10.2026, a regression of #898).
 *
 * #898 pinned the chat to the chat daemon's session: right, the chat had
 * flipped between two sessions. But an ACP agent runs a task in a session of
 * its OWN, and with the pin its delivery, tools and thoughts left the chat —
 * "as if nothing happens". This block brings them back WITHOUT a session
 * switch and without mixing them into the conversation: its own region at
 * the end of the timeline (where live things stand), under the quiet
 * "Working on <task> -> View" row that already names the task (K3: the
 * title is not repeated here).
 *
 * Content: the delivery ("Delivered 2 minutes ago"), the task session's
 * newest steps with the chat's own row components, the running thought
 * (`LiveThinkingRow`) or the answer being written, and "1 more task
 * waiting". Whether the model answers at all is the status line's job
 * (#897) — not said twice.
 */

import { useLocale, useTranslations } from "next-intl";
import { C } from "@/lib/colors";
import { timeAgo } from "@/lib/utils";
import { useTaskActivity } from "@/hooks/useTaskActivity";
import type { TimelineChatEvent } from "@/lib/chatTypes";
import { LiveThinkingRow } from "./LiveThinkingRow";
import { ThinkingRow } from "./ThinkingRow";
import { ToolRow } from "./ToolRow";

type DetailLevel = "compact" | "normal" | "verbose";

function StepRow({ ev, detailLevel }: { ev: TimelineChatEvent; detailLevel: DetailLevel }) {
  switch (ev.kind) {
    case "tool":
      return <ToolRow ev={ev} detailLevel={detailLevel} />;
    case "thinking":
      return <ThinkingRow ev={ev} detailLevel={detailLevel} />;
    case "message":
      return (
        <p className="m-0 px-4 py-1 text-xs line-clamp-3 break-words" style={{ color: C.textSecondary }}>
          {ev.text}
        </p>
      );
    default:
      return null;
  }
}

function stepKey(ev: TimelineChatEvent): string {
  return ev.kind === "tool" ? `tool:${ev.toolUseId ?? ev.uuid}` : `${ev.kind}:${ev.uuid}`;
}

export function TaskActivityBlock({
  agentId,
  detailLevel = "normal",
}: {
  agentId: string;
  /** The chat's detail setting: "Compact" hides tool and thinking steps
   *  here as it does in the chat (review UI-9); the live thought, an answer
   *  being written and the waiting tasks stay. */
  detailLevel?: DetailLevel;
}) {
  const t = useTranslations("sessions");
  const locale = useLocale();
  const activity = useTaskActivity(agentId);
  if (!activity || !activity.applies) return null;

  const { preview, queued, deliveredAt } = activity;
  const events = detailLevel === "compact"
    ? activity.events.filter((ev) => ev.kind === "message")
    : activity.events;
  return (
    <section
      data-testid="chat-task-activity"
      data-region="chat-task-activity"
      aria-label={t("taskActivityLabel")}
      className="mx-4 my-3 py-2 rounded-md border"
      style={{ borderColor: C.border, background: "var(--color-bg-surface)" }}
    >
      <div className="flex items-baseline justify-between gap-3 px-4 pb-1">
        <span className="label-sys" style={{ color: C.textMuted }}>{t("taskActivityLabel")}</span>
        {deliveredAt && (
          <span className="text-xs" style={{ color: C.textMuted }}>
            {t("taskDelivered", { time: timeAgo(deliveredAt, locale) })}
          </span>
        )}
      </div>
      {events.map((ev) => (
        <StepRow key={stepKey(ev)} ev={ev} detailLevel={detailLevel} />
      ))}
      {preview && preview.block === "thinking" && <LiveThinkingRow preview={preview} />}
      {preview && preview.block !== "thinking" && (
        <p
          data-testid="task-activity-preview"
          className="m-0 px-4 py-1 text-xs line-clamp-3 break-words"
          style={{ color: C.textSecondary }}
        >
          {preview.text}
        </p>
      )}
      {queued > 0 && (
        <p className="m-0 px-4 pt-1 text-xs" style={{ color: C.textMuted }}>
          {t("taskQueued", { count: queued })}
        </p>
      )}
    </section>
  );
}
