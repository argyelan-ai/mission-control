"use client";

import { useState, useMemo, useEffect, useRef } from "react";
import { useQuery, useQueryClient, useMutation } from "@tanstack/react-query";
import { motion, AnimatePresence } from "framer-motion";
import {
  FolderKanban,
  ChevronDown,
  ChevronRight,
  Play,
  Check,
  X,
  Trash2,
  Plus,
  Send,
  AlertTriangle,
  Clock,
  GitBranch,
  Brain,
} from "lucide-react";
import Link from "next/link";
import { useAppStore } from "@/lib/store";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";
import { Pill } from "@/components/shared/Pill";
import AppShell from "@/components/layout/AppShell";
import TaskListColumn from "@/components/tasks/TaskListColumn";
import { TaskDetailBody } from "@/components/task/TaskDetailBody";
import type { Task, TaskStatus, Agent, Project, Tag, ProjectPhase } from "@/lib/types";
import { C, LANE } from "@/lib/colors";
import { useBodyScrollLock } from "@/hooks/useBodyScrollLock";

// ── Tag Chip ───────────────────────────────────────────────────────────────

function TagChip({ tag, size = "sm" }: { tag: Tag; size?: "xs" | "sm" }) {
  const color = tag.color || C.accent;
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full font-medium",
        size === "xs" ? "text-[9px] px-1.5 py-0" : "text-[10px] px-2 py-0.5"
      )}
      style={{
        backgroundColor: `${color}18`,
        color: color,
        border: `1px solid ${color}30`,
      }}
    >
      {tag.name}
    </span>
  );
}

// ── Tag Colors ─────────────────────────────────────────────────────────────

const TAG_COLORS = [
  C.accent,
  C.online,
  C.warning,
  C.info,
  C.error,
  C.accentHover,
];

// ── Tag Manager Popover ──────────────────────────────────────────────────────

function TagManager({
  projectId,
  assignedTags,
  onClose,
}: {
  projectId: string;
  assignedTags: Tag[];
  onClose: () => void;
}) {
  // iOS-safe scroll lock while popover is open (M4)
  useBodyScrollLock(true);

  const qc = useQueryClient();
  const ref = useRef<HTMLDivElement>(null);
  const [newTagName, setNewTagName] = useState("");
  const [selectedColor, setSelectedColor] = useState(TAG_COLORS[0]);

  const { data: allTags = [] } = useQuery({
    queryKey: ["tags"],
    queryFn: api.tags.list,
  });

  const assignedIds = new Set(assignedTags.map((t) => t.id));

  const assignMutation = useMutation({
    mutationFn: (tagId: string) =>
      api.tags.assignToProject(projectId, { tag_id: tagId }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["all-project-tags"] }),
  });

  const removeMutation = useMutation({
    mutationFn: (tagId: string) =>
      api.tags.removeFromProject(projectId, tagId),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["all-project-tags"] }),
  });

  const createMutation = useMutation({
    mutationFn: (data: { name: string; color: string }) =>
      api.tags.assignToProject(projectId, { name: data.name, color: data.color }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["all-project-tags"] });
      qc.invalidateQueries({ queryKey: ["tags"] });
      setNewTagName("");
    },
  });

  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose();
    }
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, [onClose]);

  function handleToggle(tagId: string) {
    if (assignedIds.has(tagId)) removeMutation.mutate(tagId);
    else assignMutation.mutate(tagId);
  }

  function handleCreate() {
    const name = newTagName.trim();
    if (!name) return;
    createMutation.mutate({ name, color: selectedColor });
  }

  return (
    <div
      ref={ref}
      className="absolute top-full left-0 mt-1 w-56 rounded-lg shadow-xl z-50 overflow-hidden"
      role="dialog"
      aria-modal="true"
      aria-label="Manage tags"
      style={{
        backgroundColor: C.bgBase,
        border: `1px solid ${C.border}`,
        boxShadow: "0 4px 24px rgba(0,0,0,0.5), 0 1px 2px rgba(0,0,0,0.3)",
      }}
    >
      {/* Existing tags */}
      <div className="max-h-48 overflow-y-auto py-1">
        {allTags.length === 0 && (
          <div className="px-3 py-2 text-xs" style={{ color: C.textMuted }}>
            No tags yet
          </div>
        )}
        {allTags.map((tag) => (
          <button
            key={tag.id}
            onClick={() => handleToggle(tag.id)}
            className="w-full flex items-center gap-2 px-3 py-1.5 text-xs transition-colors cursor-pointer hover:bg-[rgba(255,255,255,0.05)]"
          >
            <span
              className="w-2.5 h-2.5 rounded-full shrink-0"
              style={{ backgroundColor: tag.color || C.accent }}
            />
            <span className="flex-1 text-left truncate" style={{ color: C.textPrimary }}>
              {tag.name}
            </span>
            {assignedIds.has(tag.id) && (
              <Check size={12} style={{ color: C.online }} />
            )}
          </button>
        ))}
      </div>

      {/* New tag input */}
      <div className="border-t px-3 py-2" style={{ borderColor: C.border }}>
        <div className="flex items-center gap-1.5">
          <input
            value={newTagName}
            onChange={(e) => setNewTagName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") handleCreate();
              if (e.key === "Escape") onClose();
            }}
            placeholder="New tag..."
            autoFocus
            aria-label="Create new tag"
            className="flex-1 text-xs px-2 py-1 rounded outline-none min-w-0"
            style={{
              backgroundColor: C.bgSurface,
              color: C.textPrimary,
              border: `1px solid ${C.border}`,
            }}
          />
        </div>
        <div className="flex items-center gap-1.5 mt-1.5">
          {TAG_COLORS.map((c) => (
            <button
              key={c}
              onClick={() => setSelectedColor(c)}
              className="w-4 h-4 rounded-full transition-transform cursor-pointer"
              style={{
                backgroundColor: c,
                outline: selectedColor === c ? `2px solid ${c}` : "none",
                outlineOffset: "1px",
                transform: selectedColor === c ? "scale(1.15)" : "scale(1)",
              }}
            />
          ))}
        </div>
      </div>
    </div>
  );
}

// ── Status helpers ────────────────────────────────────────────────────────────

const STATUS_CONFIG: Record<TaskStatus, { color: string; label: string }> = {
  inbox: { color: LANE.inbox, label: "Inbox" },
  in_progress: { color: LANE.in_progress, label: "Active" },
  review: { color: LANE.review, label: "Review" },
  user_test: { color: LANE.user_test, label: "User Test" },
  done: { color: LANE.done, label: "Done" },
  blocked: { color: LANE.blocked, label: "Blocked" },
  failed: { color: LANE.failed, label: "Failed" },
  aborted: { color: LANE.aborted, label: "Aborted" },
};

function TaskStatusDot({ status }: { status: TaskStatus }) {
  const icons: Partial<Record<TaskStatus, React.ReactNode>> = {
    done: <Check size={8} strokeWidth={3} className="text-white" />,
    blocked: <X size={8} strokeWidth={3} className="text-white" />,
    failed: <X size={8} strokeWidth={3} className="text-white" />,
    aborted: <X size={8} strokeWidth={3} className="text-white" />,
  };

  const color = STATUS_CONFIG[status].color;
  const isEmpty = status === "inbox";

  return (
    <span
      className="w-4 h-4 rounded-full shrink-0 flex items-center justify-center"
      style={{
        backgroundColor: isEmpty ? "transparent" : color,
        border: isEmpty ? `2px solid ${color}` : "none",
      }}
    >
      {icons[status]}
    </span>
  );
}

// ── Task Row ──────────────────────────────────────────────────────────────────

function TaskRow({
  task,
  agents,
  boardId,
  onClick,
}: {
  task: Task;
  agents: Agent[];
  boardId: string;
  onClick: () => void;
}) {
  const agent = agents.find((a) => a.id === task.assigned_agent_id);
  const qc = useQueryClient();
  const [showDoneWarning, setShowDoneWarning] = useState(false);

  const dispatchMutation = useMutation({
    mutationFn: () => api.tasks.update(boardId, task.id, { status: "in_progress" }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["tasks", boardId] });
      setShowDoneWarning(false);
    },
  });

  const canDispatch = task.status === "inbox" && task.assigned_agent_id;
  const isDone = task.status === "done";

  const staleMins = useMemo(() => {
    if (task.status !== "in_progress" || !task.last_activity_at) return 0;
    return Math.floor((Date.now() - new Date(task.last_activity_at).getTime()) / 60000);
  }, [task.status, task.last_activity_at]);
  const isStale = staleMins >= 15;
  const isCritical = staleMins >= 30;

  const priorityColor = (p: string) => {
    switch (p) {
      case "critical": return C.error;
      case "high": return C.warning;
      default: return null;
    }
  };

  const handleDispatch = (e: React.MouseEvent) => {
    e.stopPropagation();
    if (isDone) { setShowDoneWarning(true); return; }
    dispatchMutation.mutate();
  };

  const handleForceDispatch = (e: React.MouseEvent) => {
    e.stopPropagation();
    dispatchMutation.mutate();
  };

  return (
    <div className="relative">
      {/* Kein <button> als Container (nested-interactive): Titel-Button deckt
          per ::after die ganze Zeile ab, Aktionen liegen mit z-[1] darüber. */}
      <div className="w-full flex items-center gap-3 px-3 py-2 rounded-lg text-left transition-all hover:bg-[rgba(255,255,255,0.04)] group">
        <TaskStatusDot status={task.status} />
        <button
          type="button"
          onClick={onClick}
          aria-label={`Open task: ${task.title}`}
          className="flex-1 text-sm truncate flex items-center gap-1 min-w-0 text-left cursor-pointer after:absolute after:inset-0 after:content-['']"
          style={{ color: isDone ? C.textMuted : C.textPrimary }}
        >
          <span className="truncate">{task.title}</span>
          {/* Checklist-Progress Badge */}
          {task.checklist_total > 0 && (
            <span
              className="ml-1.5 px-1.5 py-0.5 rounded text-xs font-mono shrink-0"
              style={{
                background:
                  task.checklist_done === task.checklist_total
                    ? `${C.online}26`
                    : C.accentSubtle,
                color:
                  task.checklist_done === task.checklist_total
                    ? C.online
                    : C.accent,
              }}
            >
              {task.checklist_done}/{task.checklist_total}
            </span>
          )}
        </button>
        <div className="relative z-[1] flex items-center gap-2 shrink-0 opacity-60 group-hover:opacity-100 transition-opacity">
          {task.priority !== "medium" && priorityColor(task.priority) && (
            <span
              className="text-[10px] px-1 py-0.5 rounded uppercase font-semibold"
              style={{ color: priorityColor(task.priority)! }}
            >
              {task.priority}
            </span>
          )}
          {agent && (
            <span className="text-xs" title={agent.name}>
              {agent.emoji || ""}
            </span>
          )}
          {isStale && (
            <span
              className="inline-flex items-center gap-0.5 text-[10px] font-medium px-1.5 py-0.5 rounded"
              title={`No activity for ${staleMins} minutes`}
              style={{
                color: isCritical ? C.error : C.warning,
                backgroundColor: isCritical ? `${C.error}1A` : `${C.warning}1A`,
              }}
            >
              <Clock size={10} />
              {staleMins}m
            </span>
          )}
          {/* Phase E task-klammer quick-link: jump to all vault notes +
              wrappers that share this task's UUID. Hover-only so the row
              stays uncluttered for the common case where the operator just wants
              to scan the task list. */}
          <Link
            href={`/memory?task=${task.id}`}
            onClick={(e) => e.stopPropagation()}
            className="p-1 rounded transition-colors opacity-0 group-hover:opacity-100 hover:bg-[rgba(255,255,255,0.05)] cursor-pointer touch-visible"
            title="Vault: all notes + files for this task"
            style={{ color: C.textMuted }}
          >
            <Brain size={12} />
          </Link>
          {(canDispatch || isDone) && (
            <button
              onClick={handleDispatch}
              disabled={dispatchMutation.isPending}
              className="p-1 rounded transition-colors opacity-0 group-hover:opacity-100 hover:bg-[rgba(255,255,255,0.05)] cursor-pointer touch-visible"
              title={isDone ? "Task already done — dispatch again?" : "Dispatch task"}
              style={{ color: isDone ? C.warning : C.accent }}
            >
              <Send size={12} />
            </button>
          )}
        </div>
      </div>

      {/* Done warning */}
      {showDoneWarning && (
        <div
          className="absolute right-2 top-full mt-1 z-10 p-3 rounded-lg text-xs"
          style={{
            backgroundColor: C.bgBase,
            border: `1px solid ${C.warning}40`,
            boxShadow: "0 4px 24px rgba(0,0,0,0.5), 0 1px 2px rgba(0,0,0,0.3)",
          }}
        >
          <div className="flex items-center gap-1.5 mb-2 font-medium" style={{ color: C.warning }}>
            <AlertTriangle size={12} />
            Task already done
          </div>
          <p className="mb-2" style={{ color: C.textSecondary }}>
            Dispatch again anyway?
          </p>
          <div className="flex gap-2">
            <button
              onClick={handleForceDispatch}
              disabled={dispatchMutation.isPending}
              className="px-2 py-1 rounded text-[11px] font-medium cursor-pointer"
              style={{ backgroundColor: `${C.warning}1F`, color: C.warning }}
            >
              Yes, dispatch
            </button>
            <button
              onClick={(e) => { e.stopPropagation(); setShowDoneWarning(false); }}
              className="px-2 py-1 rounded text-[11px] cursor-pointer"
              style={{ color: C.textMuted }}
            >
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

// ── Phase Section ──────────────────────────────────────────────────────────────

function PhaseSection({
  phase,
  subtasks,
  agents,
  boardId,
  previousPhase,
  onTaskClick,
  repoUrl,
}: {
  phase: Task;
  subtasks: Task[];
  agents: Agent[];
  boardId: string;
  previousPhase?: Task;
  onTaskClick: (task: Task) => void;
  repoUrl?: string | null;
}) {
  const [collapsed, setCollapsed] = useState(phase.status === "done");
  const qc = useQueryClient();

  const startPhaseMutation = useMutation({
    mutationFn: () => api.tasks.update(boardId, phase.id, { status: "in_progress" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["tasks", boardId] }),
  });

  const doneCount = subtasks.filter((t) => t.status === "done").length;
  const progress = subtasks.length > 0 ? Math.round((doneCount / subtasks.length) * 100) : 0;
  const previousDone = !previousPhase || previousPhase.status === "done";
  const canStart = (phase.status === "inbox" || phase.status === "review") && previousDone;

  return (
    <div className="mb-2">
      {/* Phase header */}
      <button
        onClick={() => setCollapsed(!collapsed)}
        className="w-full flex items-center gap-2 px-3 py-2 rounded-lg transition-colors hover:bg-[rgba(255,255,255,0.04)] group cursor-pointer"
      >
        {collapsed ? (
          <ChevronRight size={14} style={{ color: C.textMuted }} />
        ) : (
          <ChevronDown size={14} style={{ color: C.textMuted }} />
        )}
        <span className="flex-1 text-left text-sm font-medium" style={{ color: C.textPrimary }}>
          {phase.title}
        </span>
        <div className="flex items-center gap-2">
          {/* Branch badge */}
          {phase.branch_name && (
            repoUrl ? (
              <a
                href={`${repoUrl}/tree/${phase.branch_name}`}
                target="_blank"
                rel="noopener noreferrer"
                onClick={(e) => e.stopPropagation()}
                className="flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-mono hover:opacity-80 transition-opacity"
                style={{
                  background: C.accentSubtle,
                  color: C.accent,
                  border: `1px solid ${C.borderAccent}`,
                }}
                title={phase.branch_name}
              >
                <GitBranch size={9} />
                <span className="max-w-[100px] truncate">{phase.branch_name}</span>
              </a>
            ) : (
              <span
                className="flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-mono"
                style={{
                  background: C.accentSubtle,
                  color: C.textMuted,
                  border: `1px solid ${C.borderSubtle}`,
                }}
                title={phase.branch_name}
              >
                <GitBranch size={9} />
                <span className="max-w-[100px] truncate">{phase.branch_name}</span>
              </span>
            )
          )}
          {subtasks.length > 0 && (
            <span className="text-xs" style={{ color: C.textMuted }}>
              {doneCount}/{subtasks.length}
            </span>
          )}
          <Pill color={STATUS_CONFIG[phase.status].color} size="sm">
            {STATUS_CONFIG[phase.status].label}
          </Pill>
        </div>
      </button>

      <AnimatePresence>
        {!collapsed && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.15 }}
            className="overflow-hidden"
          >
            <div
              className="ml-6 border-l pl-2"
              style={{ borderColor: C.border }}
            >
              {subtasks.length === 0 && (
                <div className="py-2 px-3 text-xs" style={{ color: C.textMuted }}>
                  No subtasks
                </div>
              )}
              {subtasks.map((task) => (
                <TaskRow
                  key={task.id}
                  task={task}
                  agents={agents}
                  boardId={boardId}
                  onClick={() => onTaskClick(task)}
                />
              ))}

              {/* Phase start button */}
              {canStart && (
                <button
                  onClick={() => startPhaseMutation.mutate()}
                  disabled={startPhaseMutation.isPending}
                  className="flex items-center gap-1.5 mx-3 my-2 px-3 py-1.5 rounded-lg text-xs font-medium transition-colors cursor-pointer"
                  style={{
                    backgroundColor: C.accentSubtle,
                    color: C.accentHover,
                    border: `1px solid ${C.borderAccent}`,
                  }}
                >
                  <Play size={11} fill="currentColor" />
                  {phase.status === "review"
                    ? "Finish phase & start next"
                    : "Start phase"}
                </button>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

// ── Revision Section ──────────────────────────────────────────────────────────

function RevisionSection({
  revisions,
  agents,
  boardId,
  projectId,
}: {
  revisions: Task[];
  agents: Agent[];
  boardId: string;
  projectId: string;
}) {
  const [showForm, setShowForm] = useState(false);
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [priority, setPriority] = useState("medium");
  const [assignedAgent, setAssignedAgent] = useState("");
  const queryClient = useQueryClient();

  const createRevision = useMutation({
    mutationFn: () =>
      api.tasks.create(boardId, {
        title,
        description: description || undefined,
        priority,
        task_type: "revision",
        project_id: projectId,
        assigned_agent_id: assignedAgent || undefined,
      } as Partial<Task>),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["tasks"] });
      setShowForm(false);
      setTitle("");
      setDescription("");
      setPriority("medium");
      setAssignedAgent("");
    },
  });

  return (
    <div className="mt-6 border-t pt-4" style={{ borderColor: C.border }}>
      <div className="flex items-center justify-between mb-3">
        <h3
          className="text-sm font-medium flex items-center gap-2"
          style={{ color: C.textMuted }}
        >
          Revisions
          {revisions.length > 0 && (
            <span
              className="text-xs px-1.5 py-0.5 rounded"
              style={{ backgroundColor: C.bgElevated }}
            >
              {revisions.length}
            </span>
          )}
        </h3>
        <button
          onClick={() => setShowForm(!showForm)}
          className="text-xs transition-colors cursor-pointer"
          style={{ color: C.textMuted }}
        >
          {showForm ? "Cancel" : "+ New"}
        </button>
      </div>

      {/* Create form */}
      {showForm && (
        <div
          className="mb-4 p-3 rounded-lg space-y-2"
          style={{ backgroundColor: C.bgElevated, border: `1px solid ${C.border}` }}
        >
          <input
            type="text"
            placeholder="What should change?"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            aria-label="Revision title"
            className="w-full bg-transparent rounded px-2 py-1.5 text-sm focus:outline-none"
            style={{ border: `1px solid ${C.border}`, color: C.textPrimary }}
            autoFocus
          />
          <textarea
            placeholder="Details (optional)"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            rows={2}
            aria-label="Revision description"
            className="w-full bg-transparent rounded px-2 py-1.5 text-sm focus:outline-none resize-none"
            style={{ border: `1px solid ${C.border}`, color: C.textPrimary }}
          />
          <div className="flex gap-2 items-center">
            <select
              value={priority}
              onChange={(e) => setPriority(e.target.value)}
              aria-label="Select priority"
              className="rounded px-2 py-1 text-xs cursor-pointer"
              style={{
                backgroundColor: C.bgDeep,
                border: `1px solid ${C.border}`,
                color: C.textSecondary,
              }}
            >
              <option value="low">Low</option>
              <option value="medium">Medium</option>
              <option value="high">High</option>
              <option value="critical">Critical</option>
            </select>
            <select
              value={assignedAgent}
              onChange={(e) => setAssignedAgent(e.target.value)}
              aria-label="Assign agent"
              className="rounded px-2 py-1 text-xs flex-1 cursor-pointer"
              style={{
                backgroundColor: C.bgDeep,
                border: `1px solid ${C.border}`,
                color: C.textSecondary,
              }}
            >
              <option value="">Assign agent...</option>
              {agents.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.emoji} {a.name}
                </option>
              ))}
            </select>
            <button
              onClick={() => createRevision.mutate()}
              disabled={!title.trim() || createRevision.isPending}
              className="px-3 py-1 text-xs font-medium rounded transition-colors cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed"
              style={{
                backgroundColor: C.accentSubtle,
                color: C.accentHover,
              }}
            >
              {createRevision.isPending ? "..." : "Create"}
            </button>
          </div>
        </div>
      )}

      {/* Revision list */}
      {revisions.length === 0 && !showForm && (
        <p className="text-xs italic" style={{ color: C.textMuted }}>
          No revisions
        </p>
      )}
      <div className="space-y-1">
        {revisions.map((rev) => {
          const agent = agents.find((a) => a.id === rev.assigned_agent_id);
          return (
            <div
              key={rev.id}
              className="flex items-center gap-3 px-3 py-2 rounded-lg transition-colors group hover:bg-[rgba(255,255,255,0.04)]"
            >
              <TaskStatusDot status={rev.status} />
              <span
                className="text-sm flex-1 truncate"
                style={{ color: C.textPrimary }}
              >
                {rev.title}
              </span>
              {agent && (
                <span className="text-xs" style={{ color: C.textMuted }}>
                  {agent.emoji} {agent.name}
                </span>
              )}
              <Pill color={STATUS_CONFIG[rev.status].color} size="sm">
                {STATUS_CONFIG[rev.status].label}
              </Pill>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ── Project Detail ─────────────────────────────────────────────────────────────

function ProjectDetail({
  project,
  tasks,
  agents,
  boardId,
  tags,
  onTaskClick,
}: {
  project: Project | null;
  tasks: Task[];
  agents: Agent[];
  boardId: string;
  tags: Tag[];
  onTaskClick: (task: Task) => void;
}) {
  const [showTagManager, setShowTagManager] = useState(false);

  const regularTasks = useMemo(
    () => tasks.filter((t) => t.task_type !== "revision"),
    [tasks]
  );
  const revisionTasks = useMemo(
    () => tasks.filter((t) => t.task_type === "revision"),
    [tasks]
  );

  const phases = useMemo(() => {
    const parentIds = new Set(
      regularTasks.filter((t) => t.parent_task_id).map((t) => t.parent_task_id!)
    );
    return regularTasks.filter((t) => !t.parent_task_id && parentIds.has(t.id));
  }, [regularTasks]);

  const standaloneWithProject = useMemo(() => {
    const phaseIds = new Set(phases.map((p) => p.id));
    return regularTasks.filter((t) => !t.parent_task_id && !phaseIds.has(t.id));
  }, [regularTasks, phases]);

  const subtasksFor = (phaseId: string) =>
    regularTasks.filter((t) => t.parent_task_id === phaseId);

  const totalTasks = regularTasks.filter(
    (t) => t.parent_task_id || phases.length === 0
  ).length;
  const doneTasks = regularTasks.filter(
    (t) =>
      (t.parent_task_id || phases.length === 0) && t.status === "done"
  ).length;
  const progress = totalTasks > 0 ? Math.round((doneTasks / totalTasks) * 100) : 0;

  if (!project) {
    return (
      <div className="flex-1 flex items-center justify-center">
        <div className="text-center">
          <FolderKanban
            size={32}
            className="mx-auto mb-3 opacity-20"
            style={{ color: C.textMuted }}
          />
          <div className="text-sm" style={{ color: C.textMuted }}>
            Projekt auswaehlen
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="flex-1 flex flex-col min-h-0">
      {/* Project Header */}
      <div
        className="px-6 py-4 border-b shrink-0"
        style={{ borderColor: C.border }}
      >
        <div className="flex items-center justify-between mb-2">
          <div className="flex items-center gap-2 flex-wrap min-w-0 relative">
            <h2
              className="text-lg font-semibold"
              style={{ color: C.textPrimary, letterSpacing: "-0.02em" }}
            >
              {project.name}
            </h2>
            {/* GitHub repo badge */}
            {project.github_repo_url && (
              <a
                href={project.github_repo_url}
                target="_blank"
                rel="noopener noreferrer"
                className="flex items-center gap-1 px-2 py-0.5 rounded text-[10px] font-medium shrink-0 hover:opacity-80 transition-opacity"
                style={{
                  background: C.bgSurface,
                  color: C.textMuted,
                  border: `1px solid ${C.borderSubtle}`,
                  fontFamily: "var(--font-geist-mono), monospace",
                }}
                title={project.github_repo_url}
              >
                <svg width="10" height="10" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">
                  <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"/>
                </svg>
                {project.github_repo_name ?? project.github_repo_url.split("/").slice(-2).join("/")}
              </a>
            )}
            {tags.length > 0 && (
              <div className="flex items-center gap-1 flex-wrap">
                {tags.map((tag) => (
                  <TagChip key={tag.id} tag={tag} />
                ))}
              </div>
            )}
            <button
              onClick={() => setShowTagManager(!showTagManager)}
              className="w-5 h-5 rounded-full flex items-center justify-center transition-colors hover:bg-[rgba(255,255,255,0.05)] cursor-pointer"
              style={{ color: C.textMuted }}
              title="Manage tags"
            >
              <Plus size={13} />
            </button>
            {showTagManager && (
              <TagManager
                projectId={project.id}
                assignedTags={tags}
                onClose={() => setShowTagManager(false)}
              />
            )}
          </div>
          <span
            className="text-sm font-semibold shrink-0"
            style={{
              color: progress === 100 ? C.online : C.accent,
            }}
          >
            {progress}%
          </span>
        </div>

        {/* Progress bar */}
        <div
          className="h-1.5 rounded-full overflow-hidden"
          style={{ backgroundColor: C.bgElevated }}
        >
          <div
            className="h-full rounded-full transition-all duration-500"
            style={{
              width: `${progress}%`,
              backgroundColor: progress === 100 ? C.online : C.accent,
            }}
          />
        </div>

        {project.description && (
          <p className="mt-2 text-xs" style={{ color: C.textMuted }}>
            {project.description}
          </p>
        )}
      </div>

      {/* Task list */}
      <div className="flex-1 overflow-y-auto p-4">
        {tasks.length === 0 && (
          <div
            className="text-sm text-center py-8"
            style={{ color: C.textMuted }}
          >
            No tasks in this project
          </div>
        )}

        {/* Phases */}
        {phases.map((phase, index) => (
          <PhaseSection
            key={phase.id}
            phase={phase}
            subtasks={subtasksFor(phase.id)}
            agents={agents}
            boardId={boardId}
            previousPhase={index > 0 ? phases[index - 1] : undefined}
            onTaskClick={onTaskClick}
            repoUrl={project.github_repo_url}
          />
        ))}

        {/* Standalone tasks */}
        {standaloneWithProject.length > 0 && (
          <div>
            {phases.length > 0 && (
              <div
                className="text-xs font-medium px-3 py-1 mb-1 mt-3"
                style={{ color: C.textMuted }}
              >
                Weitere Tasks
              </div>
            )}
            {standaloneWithProject.map((task) => (
              <TaskRow
                key={task.id}
                task={task}
                agents={agents}
                boardId={boardId}
                onClick={() => onTaskClick(task)}
              />
            ))}
          </div>
        )}

        {/* Revisions */}
        <RevisionSection
          revisions={revisionTasks}
          agents={agents}
          boardId={boardId}
          projectId={project.id}
        />
      </div>
    </div>
  );
}

// ── Main Page (Redesign 07/2026 — task list first, projects as groups) ─────────
//
// The task is the primary unit: TaskListColumn sits next to the sidebar,
// grouped by status (operational) or project (structural, Ad-hoc first).
// The right pane switches between task detail (TaskDetailBody), the
// project/phase view (ProjectDetail via the group header link) and an
// empty state. Mobile keeps the stack navigation (list → detail).

function TasksPageContent() {
  const { activeBoardId } = useAppStore();
  const qc = useQueryClient();
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const [projectViewId, setProjectViewId] = useState<string | null>(null);
  const [confirmDeleteProject, setConfirmDeleteProject] = useState(false);
  // Mobile (<md) stack navigation: which pane fills the screen. Desktop (≥md)
  // always shows the split. Default "list" = mobile lands on the task list,
  // detail only after a tap (iPhone-Befund Operator).
  const [mobileView, setMobileView] = useState<"list" | "detail">("list");

  const { data: allTasks = [] } = useQuery({
    queryKey: ["tasks", activeBoardId],
    queryFn: () => api.tasks.list(activeBoardId!),
    enabled: !!activeBoardId,
    refetchInterval: 15_000,
  });

  const { data: agents = [] } = useQuery({
    queryKey: ["agents", activeBoardId],
    queryFn: () => api.agents.list(activeBoardId!),
    enabled: !!activeBoardId,
  });

  const { data: projects = [] } = useQuery({
    queryKey: ["projects", activeBoardId],
    queryFn: () => api.projects.list(activeBoardId!),
    enabled: !!activeBoardId,
  });

  // Tags only matter for the project view header
  const { data: projectTags = [] } = useQuery({
    queryKey: ["project-tags", projectViewId],
    queryFn: () => api.tags.forProject(projectViewId!),
    enabled: !!projectViewId,
  });

  const deleteProjectMutation = useMutation({
    mutationFn: (projectId: string) => api.projects.delete(activeBoardId!, projectId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["projects", activeBoardId] });
      qc.invalidateQueries({ queryKey: ["tasks", activeBoardId] });
      setProjectViewId(null);
      setConfirmDeleteProject(false);
      setMobileView("list");
    },
  });

  // Selected task always derives fresh from the query cache — a status change
  // in the detail header must not render against a stale snapshot.
  const selectedTask = useMemo(
    () => (selectedTaskId ? (allTasks.find((t) => t.id === selectedTaskId) ?? null) : null),
    [allTasks, selectedTaskId],
  );

  const projectView = projectViewId ? (projects.find((p) => p.id === projectViewId) ?? null) : null;
  const projectViewTasks = useMemo(
    () => (projectViewId ? allTasks.filter((t) => t.project_id === projectViewId) : []),
    [allTasks, projectViewId],
  );

  function handleSelectTask(task: Task) {
    setSelectedTaskId(task.id);
    setProjectViewId(null);
    setMobileView("detail");
  }

  function handleOpenProject(projectId: string) {
    setProjectViewId(projectId);
    setSelectedTaskId(null);
    setConfirmDeleteProject(false);
    setMobileView("detail");
  }

  function handleCloseDetail() {
    setSelectedTaskId(null);
    setProjectViewId(null);
    setMobileView("list");
  }

  if (!activeBoardId) {
    return (
      <div className="flex items-center justify-center h-full">
        <div className="text-sm" style={{ color: C.textMuted }}>
          No board selected
        </div>
      </div>
    );
  }

  const detailOpen = !!selectedTask || !!projectView;

  return (
    <div className="flex md:-m-6 md:h-[calc(100dvh-theme(spacing.6)*2)]">
      {/* ── Task list (primary column) ── */}
      <div
        className={`${mobileView === "list" ? "flex" : "hidden"} md:flex flex-1 md:flex-none md:w-[340px] md:border-r min-h-0`}
        style={{ borderColor: C.border }}
      >
        <TaskListColumn
          tasks={allTasks}
          projects={projects}
          agents={agents}
          boardId={activeBoardId}
          selectedTaskId={selectedTaskId}
          onSelectTask={handleSelectTask}
          onOpenProject={handleOpenProject}
        />
      </div>

      {/* ── Right pane: task detail / project view / empty state ── */}
      <div className={`${mobileView === "detail" ? "flex" : "hidden"} md:flex flex-1 flex-col min-h-0 min-w-0`}>
        {/* Mobile: back to the list */}
        {detailOpen && (
          <div
            className="flex items-center gap-3 px-4 py-3 border-b shrink-0 md:hidden"
            style={{ borderColor: C.border }}
          >
            <button
              onClick={handleCloseDetail}
              className="flex items-center gap-2 px-3 py-2 rounded-lg text-sm font-medium transition-colors cursor-pointer min-h-[44px]"
              style={{
                backgroundColor: C.bgSurface,
                color: C.textSecondary,
                border: `1px solid ${C.border}`,
              }}
            >
              <ChevronRight size={14} className="rotate-180" style={{ color: C.textMuted }} />
              Tasks
            </button>
            <span className="text-sm truncate" style={{ color: C.textPrimary }}>
              {selectedTask?.title ?? projectView?.name ?? ""}
            </span>
          </div>
        )}

        {selectedTask ? (
          <div className="flex-1 flex flex-col min-h-0" style={{ backgroundColor: C.bgBase }}>
            <TaskDetailBody
              task={selectedTask}
              agents={agents}
              boardId={activeBoardId}
              onClose={handleCloseDetail}
            />
          </div>
        ) : projectView ? (
          <div className="flex-1 flex flex-col min-h-0">
            {/* Project pane header: back to list context + delete */}
            <div
              className="hidden md:flex items-center gap-2 px-6 py-2 border-b shrink-0"
              style={{ borderColor: C.border }}
            >
              <button
                onClick={handleCloseDetail}
                className="text-[11px] px-2 py-1 rounded-md cursor-pointer transition-colors hover:bg-[rgba(255,255,255,0.05)]"
                style={{ color: C.textMuted, border: `1px solid ${C.border}` }}
              >
                ← All tasks
              </button>
              <span className="text-[11px]" style={{ color: C.textDim }}>
                Project view
              </span>
              <span className="ml-auto flex items-center gap-2">
                {confirmDeleteProject ? (
                  <>
                    <span className="text-[11px]" style={{ color: C.warning }}>
                      Delete project? Tasks stay (become ad-hoc).
                    </span>
                    <button
                      onClick={() => deleteProjectMutation.mutate(projectView.id)}
                      disabled={deleteProjectMutation.isPending}
                      className="px-2 py-1 rounded text-[11px] font-semibold cursor-pointer"
                      style={{ backgroundColor: `${C.error}26`, color: "#D05F5F" }}
                    >
                      {deleteProjectMutation.isPending ? "…" : "Delete project"}
                    </button>
                    <button
                      onClick={() => setConfirmDeleteProject(false)}
                      className="px-2 py-1 rounded text-[11px] cursor-pointer"
                      style={{ color: C.textMuted }}
                    >
                      Cancel
                    </button>
                  </>
                ) : (
                  <button
                    onClick={() => setConfirmDeleteProject(true)}
                    aria-label="Delete project"
                    title="Delete project"
                    className="p-1.5 rounded-md cursor-pointer transition-colors hover:bg-[rgba(255,255,255,0.05)]"
                    style={{ color: C.textMuted }}
                  >
                    <Trash2 size={13} />
                  </button>
                )}
              </span>
            </div>
            <ProjectDetail
              project={projectView}
              tasks={projectViewTasks}
              agents={agents}
              boardId={activeBoardId}
              tags={projectTags}
              onTaskClick={handleSelectTask}
            />
          </div>
        ) : (
          <div className="flex-1 hidden md:flex items-center justify-center">
            <div className="text-center">
              <div className="text-sm" style={{ color: C.textMuted }}>
                Select a task from the list
              </div>
              <div className="text-xs mt-1" style={{ color: C.textDim }}>
                Group by project to reach the phase view of a project
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

export default function TasksPage() {
  return (
    <AppShell>
      <TasksPageContent />
    </AppShell>
  );
}
