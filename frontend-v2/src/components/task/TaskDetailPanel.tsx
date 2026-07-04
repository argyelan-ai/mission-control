"use client";

/**
 * TaskDetailPanel — chrome around <TaskDetailBody> (07/2026 redesign).
 *
 * Two variants share one body:
 *  - "panel": embedded side column (used by the /tasks split view)
 *  - "modal": centered dialog with backdrop (used by pipeline / lists)
 *
 * All content, queries and mutations live in TaskDetailBody — the previous
 * ~180-line duplication between the two variants is gone.
 */

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

export default function TaskDetailPanel({
  task,
  agents,
  boardId,
  onClose,
  variant,
}: TaskDetailPanelProps) {
  // iOS-safe scroll lock — only in modal variant (M4); panel variant is embedded in layout
  useBodyScrollLock(variant === "modal");

  if (variant === "panel") {
    return (
      <motion.div
        key={task.id}
        initial={{ opacity: 0, x: 24 }}
        animate={{ opacity: 1, x: 0 }}
        exit={{ opacity: 0, x: 24 }}
        transition={{ duration: 0.2, ease: [0.16, 1, 0.3, 1] }}
        className="w-[420px] shrink-0 border-l flex flex-col overflow-hidden"
        style={{ borderColor: C.borderActive, backgroundColor: C.bgBase }}
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
          backgroundColor: "rgba(0, 0, 0, 0.6)",
          backdropFilter: "blur(8px)",
          WebkitBackdropFilter: "blur(8px)",
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
        {/* Centered panel */}
        <motion.div
          initial={{ opacity: 0, y: "100%" }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: "100%" }}
          transition={{ duration: 0.28, ease: [0.16, 1, 0.3, 1] }}
          className="relative w-full rounded-t-2xl sm:rounded-2xl sm:max-w-2xl flex flex-col z-[51] overflow-hidden"
          role="dialog"
          aria-modal="true"
          aria-label="Task details"
          style={{
            maxHeight: "calc(100dvh - env(safe-area-inset-top) - 5.5rem)",
            backgroundColor: C.bgBase,
            border: `1px solid ${C.borderActive}`,
            boxShadow: "0 4px 24px rgba(0,0,0,0.5), 0 1px 2px rgba(0,0,0,0.3)",
          }}
          onClick={(e) => e.stopPropagation()}
        >
          {/* Drag handle (mobile sheet affordance) */}
          <div className="sm:hidden flex justify-center pt-2 pb-1 shrink-0">
            <div className="w-9 h-1 rounded-full" style={{ backgroundColor: "rgba(255,255,255,0.15)" }} />
          </div>
          <TaskDetailBody task={task} agents={agents} boardId={boardId} onClose={onClose} />
        </motion.div>
      </motion.div>
    </>
  );
}
