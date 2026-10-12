"use client";

/**
 * TodoPanel — the agent's todo list, pinned between the conversation and the
 * status line. Fed by the harness-neutral `todo` snapshot (Claude Code
 * `TodoWrite`, ACP `plan`; backend `chat_todos.py`), newest snapshot wins.
 *
 * Collapsed by default (phone first): ONE line, "3/7 done · <current item>",
 * which is the answer to "where is it?" without giving up the conversation's
 * height. Tapping opens the full list — the same `ChecklistItems` component the
 * task detail uses, because with the todo → checklist sync it is the same list.
 * No snapshot (or an empty one) → nothing at all, never an empty frame.
 */
import { useId, useState } from "react";
import { ChevronDown, ListChecks } from "lucide-react";
import { useTranslations } from "next-intl";
import { C } from "@/lib/colors";
import type { TodoEvent } from "@/lib/chatTypes";
import { ChecklistItems, currentChecklistItem, todoItemsToChecklist } from "@/components/shared/ChecklistItems";

export function TodoPanel({ todo }: { todo: TodoEvent | null }) {
  const t = useTranslations("sessions.todo");
  const [open, setOpen] = useState(false);
  const listId = useId();
  if (!todo || todo.items.length === 0) return null;

  const items = todoItemsToChecklist(todo.items);
  const total = items.length;
  const done = items.filter((i) => i.status === "done").length;
  const current = currentChecklistItem(items);

  return (
    // Left edge = the message column, like the status line below.
    <div
      data-region="chat-todo"
      data-testid="todo-panel"
      className="px-4 md:px-5"
      style={{ borderTop: `1px solid ${C.borderSubtle}` }}
    >
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        aria-controls={listId}
        aria-label={`${t("panelLabel", { done, total })}. ${t(open ? "hideList" : "showList")}`}
        className="group flex w-full min-h-11 items-center gap-2 text-xs text-left cursor-pointer rounded-md focus-visible:outline-none focus-visible:ring-1"
      >
        <ListChecks size={14} aria-hidden="true" className="shrink-0" style={{ color: C.textMuted }} />
        <span data-testid="todo-progress" className="shrink-0 tabular-nums font-medium" style={{ color: C.textSecondary }}>
          {t("progress", { done, total })}
        </span>
        <span aria-hidden="true" style={{ color: C.textMuted }}>·</span>
        <span data-testid="todo-current" className="min-w-0 flex-1 truncate group-hover:underline" style={{ color: current ? C.textPrimary : C.textMuted }}>
          {current ? current.title : t("allDone")}
        </span>
        <ChevronDown
          size={14}
          aria-hidden="true"
          className="shrink-0"
          style={{ color: C.textMuted, transform: open ? "rotate(180deg)" : "none", transition: "transform 0.15s" }}
        />
      </button>
      {open && (
        <div id={listId} className="max-h-48 overflow-y-auto scroll-quiet pb-3 text-xs">
          <ChecklistItems items={items} />
        </div>
      )}
    </div>
  );
}
