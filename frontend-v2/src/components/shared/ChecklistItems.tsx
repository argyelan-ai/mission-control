"use client";

/**
 * ChecklistItems — the ONE list renderer for a task's checklist and an agent's
 * todo snapshot. The task detail (Summary tab → Plan) and the chat's todo panel
 * render the same component, because since the todo → checklist sync
 * (backend `checklist_todo_sync.py`) they show the same list.
 *
 * The current item (the first `in_progress`, else the first open one) is the one
 * coloured thing: info tone + medium weight + `aria-current="step"`. Finished
 * rows recede (muted check), so a long list reads as "where are we" at a glance
 * (K1, K6: one status colour per region; `blocked` is the rare exception that
 * must stand out).
 */
import { AlertCircle, CheckSquare, Square, SquareDot, SquareMinus } from "lucide-react";
import { useTranslations } from "next-intl";
import { C } from "@/lib/colors";
import type { TodoItem } from "@/lib/chatTypes";

export type ChecklistStatus = "pending" | "in_progress" | "done" | "blocked" | "skipped";

export interface ChecklistEntry {
  id: string;
  title: string;
  status: ChecklistStatus;
}

/** The item the agent is on: the first one in progress, else the next open one. */
export function currentChecklistItem<T extends ChecklistEntry>(items: T[]): T | null {
  return items.find((i) => i.status === "in_progress") ?? items.find((i) => i.status === "pending") ?? null;
}

const TODO_STATUS: Record<TodoItem["status"], ChecklistStatus> = {
  pending: "pending",
  in_progress: "in_progress",
  completed: "done",
};

/** A todo snapshot (chat event) as checklist entries — same order, same words. */
export function todoItemsToChecklist(items: TodoItem[]): ChecklistEntry[] {
  return items.map((item, i) => ({ id: `todo-${i}`, title: item.content, status: TODO_STATUS[item.status] ?? "pending" }));
}

const STATUS_WORD: Record<ChecklistStatus, string> = {
  pending: "statusPending",
  in_progress: "statusInProgress",
  done: "statusDone",
  blocked: "statusBlocked",
  skipped: "statusSkipped",
};

const ICON: Record<ChecklistStatus, { Icon: typeof Square; color: string }> = {
  done: { Icon: CheckSquare, color: C.textMuted },
  in_progress: { Icon: SquareDot, color: C.info },
  blocked: { Icon: AlertCircle, color: C.error },
  skipped: { Icon: SquareMinus, color: C.textMuted },
  pending: { Icon: Square, color: C.textMuted },
};

function StatusIcon({ status }: { status: ChecklistStatus }) {
  const { Icon, color } = ICON[status] ?? ICON.pending;
  // mt-1 sits the 12 px icon on the first text line when a title wraps.
  return <Icon size={12} aria-hidden="true" className="shrink-0 mt-1" style={{ color }} />;
}

export function ChecklistItems({ items, className }: { items: ChecklistEntry[]; className?: string }) {
  const t = useTranslations("sessions.todo");
  const current = currentChecklistItem(items);
  return (
    <ul className={`flex flex-col gap-1 ${className ?? ""}`}>
      {items.map((item) => {
        const isCurrent = item.id === current?.id;
        const finished = item.status === "done" || item.status === "skipped";
        return (
          <li
            key={item.id}
            data-status={item.status}
            aria-current={isCurrent ? "step" : undefined}
            className="flex items-start gap-2 min-w-0"
          >
            <StatusIcon status={item.status} />
            <span
              className={`min-w-0 break-words ${isCurrent ? "font-medium" : ""}`}
              style={{ color: finished ? C.textMuted : C.textPrimary }}
            >
              {item.title}
              <span className="sr-only"> — {t(STATUS_WORD[item.status])}</span>
            </span>
          </li>
        );
      })}
    </ul>
  );
}
