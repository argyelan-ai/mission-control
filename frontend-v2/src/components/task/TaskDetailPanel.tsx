"use client";

/**
 * TaskDetailPanel — chrome around <TaskDetailBody> (07/2026 redesign, 09/2026
 * cockpit width).
 *
 * Two variants share one body:
 *  - "panel": embedded side column (used by the /tasks split view) — narrow,
 *    single column.
 *  - "modal": centered dialog with backdrop (used by pipeline / lists). On a
 *    desktop viewport (≥ 1024px) the modal opens wide and the body renders
 *    its two-column cockpit (story | changes + results).
 *
 * All content, queries and mutations live in TaskDetailBody — the previous
 * ~180-line duplication between the two variants is gone.
 */

import { useEffect, useState } from "react";
import { motion } from "framer-motion";
import { useBodyScrollLock } from "@/hooks/useBodyScrollLock";
import { C } from "@/lib/colors";
import { TaskDetailBody } from "./TaskDetailBody";
import type { Task, Agent } from "@/lib/types";

interface TaskDetailPanelProps {
  task: Task;
  agents: Agent[];
  boardId: string;
  onClose: () => void;
  variant?: "modal" | "panel";
}

const WIDE_QUERY = "(min-width: 1024px)";

function useWideViewport(): boolean {
  const [wide, setWide] = useState(false);
  useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return;
    const mq = window.matchMedia(WIDE_QUERY);
    const update = () => setWide(mq.matches);
    update();
    mq.addEventListener("change", update);
    return () => mq.removeEventListener("change", update);
  }, []);
  return wide;
}

export default function TaskDetailPanel({
  task,
  agents,
  boardId,
  onClose,
  variant,
}: TaskDetailPanelProps) {
  const wide = useWideViewport() && variant !== "panel";

  // iOS-safe scroll lock — only in modal variant (M4); panel variant is embedded in layout
  useBodyScrollLock(variant === "modal");

  // Esc closes the modal variant. Portal menus (status/assignee dropdowns,
  // rendered with role=menu/listbox) handle Escape themselves — don't close
  // the panel out from under an open menu.
  useEffect(() => {
    if (variant === "panel") return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      if (document.querySelector('[role="menu"], [role="listbox"]')) return;
      onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [variant, onClose]);

  if (variant === "panel") {
    return (
      <motion.div
        key={task.id}
        initial={{ opacity: 0, x: 24 }}
        animate={{ opacity: 1, x: 0 }}
        exit={{ opacity: 0, x: 24 }}
        transition={{ duration: 0.2, ease: [0.16, 1, 0.3, 1] }}
        className="w-[420px] max-w-[calc(100vw-2rem)] shrink-0 border-l flex flex-col overflow-hidden"
        style={{ borderColor: C.border, backgroundColor: C.bgBase }}
      >
        <TaskDetailBody task={task} agents={agents} boardId={boardId} onClose={onClose} />
      </motion.div>
    );
  }

  return (
    <>
      {/* Backdrop overlay */}
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        transition={{ duration: 0.15 }}
        className="fixed inset-0 z-50 flex items-end sm:items-center justify-center sm:p-8"
        style={{
          backgroundColor: "rgba(2, 4, 8, 0.7)",
          paddingTop: "calc(env(safe-area-inset-top) + 3.5rem)",
          paddingBottom: "env(safe-area-inset-bottom)",
          paddingLeft: "env(safe-area-inset-left)",
          paddingRight: "env(safe-area-inset-right)",
          touchAction: "none",
        }}
        onClick={(e) => {
          if (e.target === e.currentTarget) onClose();
        }}
      >
        {/* Centered panel — wide on desktop for the two-column cockpit */}
        <motion.div
          initial={{ opacity: 0, y: "100%" }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: "100%" }}
          transition={{ duration: 0.28, ease: [0.16, 1, 0.3, 1] }}
          className={`relative w-full rounded-t-lg sm:rounded-lg flex flex-col z-[51] overflow-hidden ${wide ? "sm:max-w-5xl" : "sm:max-w-2xl"}`}
          role="dialog"
          aria-modal="true"
          aria-label="Task details"
          style={{
            maxHeight: "calc(100dvh - env(safe-area-inset-top) - 5.5rem)",
            backgroundColor: C.bgBase,
            border: `1px solid ${C.border}`,
            boxShadow: "var(--shadow-elevated)",
          }}
          onClick={(e) => e.stopPropagation()}
        >
          {/* Drag handle (mobile sheet affordance) */}
          <div className="sm:hidden flex justify-center pt-2 pb-1 shrink-0">
            <div className="w-9 h-1 rounded-sm" style={{ backgroundColor: "var(--color-bg-hover)" }} />
          </div>
          <TaskDetailBody task={task} agents={agents} boardId={boardId} onClose={onClose} wide={wide} />
        </motion.div>
      </motion.div>
    </>
  );
}
