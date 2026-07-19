"use client";

import { useState, useCallback } from "react";
import { useQuery } from "@tanstack/react-query";
import { AnimatePresence } from "framer-motion";
import {
  Inbox,
  Play,
  ClipboardCheck,
  Smartphone,
  MessageCircleQuestion,
  AlertTriangle,
  XCircle,
  Ban,
  CheckCircle2,
  Loader2,
} from "lucide-react";
import { api } from "@/lib/api";
import { PipelineCard } from "./PipelineCard";
import TaskDetailPanel from "@/components/task/TaskDetailPanel";
import type { Agent, PipelineTask, Task } from "@/lib/types";
import { C, LANE } from "@/components/homepage/colors";

// ── Lane Config (colors sourced from the single LANE vocabulary in colors.ts) ──

const LANES = [
  { key: "inbox",       label: "Inbox",       icon: Inbox               },
  { key: "in_progress", label: "In Progress", icon: Play                },
  { key: "waiting",     label: "Waiting",     icon: MessageCircleQuestion },
  { key: "review",      label: "Review",      icon: ClipboardCheck      },
  { key: "user_test",   label: "User Test",   icon: Smartphone          },
  { key: "blocked",     label: "Blocked",     icon: AlertTriangle       },
  { key: "failed",      label: "Failed",      icon: XCircle             },
  { key: "aborted",     label: "Aborted",     icon: Ban                 },
  { key: "done",        label: "Done",        icon: CheckCircle2        },
] as const;

type LaneKey = (typeof LANES)[number]["key"];

// ── PipelineView ──────────────────────────────────────────────────────────────

interface PipelineViewProps {
  boardId: string;
  agents?: Agent[];
}

export default function PipelineView({ boardId, agents }: PipelineViewProps) {
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);

  // Pipeline query
  const { data } = useQuery({
    queryKey: ["pipeline", boardId],
    queryFn: () => api.tasks.pipeline(boardId),
    enabled: !!boardId,
    refetchInterval: 30_000,
  });

  // Done tasks (max 5 recent)
  const { data: doneTasks } = useQuery({
    queryKey: ["tasks", boardId, "done"],
    queryFn: () => api.tasks.list(boardId, { status: "done" }),
    enabled: !!boardId,
    refetchInterval: 30_000,
  });

  // Full task for detail panel
  const { data: selectedTask, isLoading: selectedTaskLoading } = useQuery({
    queryKey: ["task", boardId, selectedTaskId],
    queryFn: () => api.tasks.get(boardId, selectedTaskId!),
    enabled: !!selectedTaskId,
  });

  // Horizontal drag-to-scroll handler
  const handleMouseDown = useCallback((e: React.MouseEvent<HTMLDivElement>) => {
    if (e.button !== 0) return;
    const el = e.currentTarget;
    if (el.scrollWidth <= el.clientWidth) return;
    const startX = e.pageX;
    const startScroll = el.scrollLeft;
    let dragged = false;
    el.style.cursor = "grabbing";

    const onMove = (ev: MouseEvent) => {
      const dx = ev.pageX - startX;
      if (Math.abs(dx) > 3) dragged = true;
      el.scrollLeft = startScroll - dx;
    };
    const onUp = () => {
      el.style.cursor = "grab";
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
      if (dragged) {
        const blocker = (ev: Event) => {
          ev.stopPropagation();
          ev.preventDefault();
        };
        el.addEventListener("click", blocker, { capture: true, once: true });
      }
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  }, []);

  if (!data) return null;

  const { pipeline, done_count, failed_count } = data;
  const totalActive = Object.values(pipeline).reduce(
    (sum, col) => sum + col.length,
    0
  );

  if (totalActive === 0 && done_count === 0) return null;

  // Convert done tasks to PipelineTask format
  const doneAsPipeline: PipelineTask[] = (doneTasks ?? [])
    .sort(
      (a: Task, b: Task) =>
        new Date(b.completed_at ?? b.updated_at).getTime() -
        new Date(a.completed_at ?? a.updated_at).getTime()
    )
    .slice(0, 5)
    .map((t: Task & { tags?: { name: string; color: string | null }[] }) => ({
      id: t.id,
      title: t.title,
      priority: t.priority,
      parent_task_id: t.parent_task_id,
      agent: (() => {
        const a = agents?.find((ag) => ag.id === t.assigned_agent_id);
        return a ? { name: a.name, emoji: a.emoji ?? "" } : null;
      })(),
      has_blocked_deps: false,
      tags: t.tags,
    }));

  // Build lane task map
  const laneTaskMap: Record<LaneKey, PipelineTask[]> = {
    inbox: pipeline.inbox ?? [],
    in_progress: pipeline.in_progress ?? [],
    review: pipeline.review ?? [],
    user_test: pipeline.user_test ?? [],
    waiting: pipeline.waiting ?? [],
    blocked: pipeline.blocked ?? [],
    failed: pipeline.failed ?? [],
    aborted: pipeline.aborted ?? [],
    done: doneAsPipeline,
  };

  // Visible lanes: hide empty ones, except done (show if done_count > 0)
  const visibleLanes = LANES.filter((lane) => {
    if (lane.key === "done") return done_count > 0;
    return laneTaskMap[lane.key].length > 0;
  });

  return (
    <>
      <div className="flex flex-col gap-4">
        {/* Header */}
        <div className="flex items-center justify-between">
          <h2
            className="text-xs font-semibold uppercase tracking-[0.08em]"
            style={{ color: C.textMuted }}
          >
            Pipeline
          </h2>
          <div className="flex items-center gap-3 text-xs" style={{ color: C.textMuted }}>
            {failed_count > 0 && (
              <span className="flex items-center gap-1">
                <XCircle size={11} style={{ color: C.error }} />
                {failed_count} failed
              </span>
            )}
          </div>
        </div>

        {/* Swim Lanes */}
        {visibleLanes.length === 0 ? (
          <div className="text-sm text-center py-6" style={{ color: C.textMuted }}>
            No active tasks.
          </div>
        ) : (
          <div className="flex flex-col gap-5">
            {visibleLanes.map((lane) => {
              const tasks = laneTaskMap[lane.key];
              const Icon = lane.icon;
              const laneColor = LANE[lane.key];

              return (
                <div key={lane.key}>
                  {/* Lane header */}
                  <div className="flex items-center gap-2 mb-2.5 px-0.5">
                    <Icon size={13} style={{ color: laneColor }} />
                    <span
                      className="text-[11px] font-semibold uppercase tracking-[0.06em]"
                      style={{ color: laneColor }}
                    >
                      {lane.label}
                    </span>
                    <span
                      className="text-[11px] font-medium px-1.5 py-0.5 rounded-full"
                      style={{
                        color: laneColor,
                        backgroundColor: `${laneColor}1A`,
                      }}
                    >
                      {lane.key === "done" ? done_count : tasks.length}
                    </span>
                  </div>

                  {/* Horizontal scroll row */}
                  <div
                    className="flex gap-2.5 overflow-x-auto pb-1"
                    style={{ scrollbarWidth: "none", cursor: "grab" }}
                    onWheel={(e) => {
                      const el = e.currentTarget;
                      if (el.scrollWidth <= el.clientWidth) return;
                      e.preventDefault();
                      el.scrollLeft += e.deltaY;
                    }}
                    onMouseDown={handleMouseDown}
                  >
                    {tasks.map((task) => (
                      <PipelineCard
                        key={task.id}
                        task={task}
                        laneKey={lane.key}
                        onClick={() => setSelectedTaskId(task.id)}
                      />
                    ))}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>

      {/* Loading indicator when task is being fetched */}
      {selectedTaskId && selectedTaskLoading && !selectedTask && (
        <div
          className="fixed right-0 top-0 h-full w-full md:w-[420px] max-w-full border-l flex items-center justify-center z-40"
          style={{
            backgroundColor: C.bgDeep,
            borderColor: C.border,
          }}
        >
          <Loader2 size={24} className="animate-spin" style={{ color: C.textMuted }} />
        </div>
      )}

      {/* Task Detail Panel */}
      <AnimatePresence>
        {selectedTask && (
          <TaskDetailPanel
            task={selectedTask}
            agents={agents ?? []}
            boardId={boardId}
            onClose={() => setSelectedTaskId(null)}
          />
        )}
      </AnimatePresence>
    </>
  );
}
