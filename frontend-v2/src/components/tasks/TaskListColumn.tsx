"use client";

/**
 * TaskListColumn — the primary task list of the /tasks page (Redesign 07/2026).
 *
 * The task is the primary unit of the page. One flat, grouped list next to
 * the sidebar; projects act as groups/filters, not as containers. Ad-hoc
 * tasks are a first-class group instead of a hidden toggle.
 *
 * Grouping modes:
 *  - "status": operational view (what needs attention?) — lanes as sections
 *  - "project": structural view — Ad-hoc first, then projects incl. progress
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { Brain, Check, ChevronRight, Clock, Paperclip, Search, Send, X, Zap } from "lucide-react";
import { api } from "@/lib/api";
import { C, LANE } from "@/lib/colors";
import type { Agent, Project, Task, TaskStatus } from "@/lib/types";
import { ProjectReferencesDialog } from "./ProjectReferencesDialog";

// ── Status vocabulary ────────────────────────────────────────────────────────

const STATUS_ORDER: TaskStatus[] = [
  "in_progress",
  "review",
  "user_test",
  "blocked",
  "failed",
  "inbox",
  "aborted",
  "done",
];

const STATUS_LABEL: Record<TaskStatus, string> = {
  inbox: "Inbox",
  in_progress: "In Progress",
  review: "Review",
  user_test: "User Test",
  blocked: "Blocked",
  failed: "Failed",
  aborted: "Aborted",
  done: "Done",
};

function StatusDot({ status }: { status: TaskStatus }) {
  const icons: Partial<Record<TaskStatus, React.ReactNode>> = {
    done: <Check size={8} strokeWidth={3} className="text-white" />,
    blocked: <X size={8} strokeWidth={3} className="text-white" />,
    failed: <X size={8} strokeWidth={3} className="text-white" />,
    aborted: <X size={8} strokeWidth={3} className="text-white" />,
  };
  const color = LANE[status] ?? C.textMuted;
  const outline = status === "inbox";
  return (
    <span
      className="w-3.5 h-3.5 rounded-full shrink-0 flex items-center justify-center"
      style={{
        backgroundColor: outline ? "transparent" : color,
        border: outline ? `2px solid ${color}` : "none",
      }}
    >
      {icons[status]}
    </span>
  );
}

// ── Row ──────────────────────────────────────────────────────────────────────

function ListRow({
  task,
  agents,
  boardId,
  selected,
  showProject,
  projectName,
  onClick,
}: {
  task: Task;
  agents: Agent[];
  boardId: string;
  selected: boolean;
  showProject: boolean;
  projectName: string | null;
  onClick: () => void;
}) {
  const qc = useQueryClient();
  const agent = agents.find((a) => a.id === task.assigned_agent_id);
  const isDone = task.status === "done";

  const dispatchMutation = useMutation({
    mutationFn: () => api.tasks.update(boardId, task.id, { status: "in_progress" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["tasks", boardId] }),
  });
  const canDispatch = task.status === "inbox" && !!task.assigned_agent_id;

  const staleMins = useMemo(() => {
    if (task.status !== "in_progress" || !task.last_activity_at) return 0;
    return Math.floor((Date.now() - new Date(task.last_activity_at).getTime()) / 60000);
  }, [task.status, task.last_activity_at]);
  const isStale = staleMins >= 15;
  const isCritical = staleMins >= 30;

  return (
    <div className="relative group">
      <div
        className="w-full flex items-center gap-2.5 px-3 py-2 rounded-lg transition-colors"
        style={selected ? { backgroundColor: C.accentSubtle, boxShadow: `inset 0 0 0 1px ${C.borderAccent}` } : undefined}
      >
        <StatusDot status={task.status} />
        <button
          type="button"
          onClick={onClick}
          aria-label={`Open task: ${task.title}`}
          className="flex-1 min-w-0 text-left text-[13px] truncate cursor-pointer after:absolute after:inset-0 after:content-[''] hover:opacity-90"
          style={{ color: isDone ? C.textMuted : C.textPrimary }}
        >
          <span className="truncate">{task.title}</span>
        </button>
        <div className="relative z-[1] flex items-center gap-1.5 shrink-0">
          {task.priority === "critical" || task.priority === "high" ? (
            <span
              className="text-[9px] px-1 rounded uppercase font-semibold"
              style={{ color: task.priority === "critical" ? C.error : C.warning }}
            >
              {task.priority}
            </span>
          ) : null}
          {isStale && (
            <span
              className="inline-flex items-center gap-0.5 text-[10px] font-medium px-1 py-0.5 rounded"
              title={`No activity for ${staleMins} minutes`}
              style={{
                color: isCritical ? C.error : C.warning,
                backgroundColor: isCritical ? `${C.error}1A` : `${C.warning}1A`,
              }}
            >
              <Clock size={9} />
              {staleMins}m
            </span>
          )}
          {showProject && (
            <span
              className="text-[9px] px-1.5 py-px rounded truncate max-w-[88px]"
              style={{ color: C.textDim, border: `1px solid ${C.border}` }}
            >
              {projectName ?? "Ad-hoc"}
            </span>
          )}
          {agent && (
            <span className="text-xs" title={agent.name}>
              {agent.emoji || "🤖"}
            </span>
          )}
          <Link
            href={`/memory?task=${task.id}`}
            onClick={(e) => e.stopPropagation()}
            className="p-1 rounded opacity-0 group-hover:opacity-100 transition-opacity hover:bg-[rgba(255,255,255,0.05)] cursor-pointer touch-visible"
            title="Vault: all notes and files for this task"
            style={{ color: C.textMuted }}
          >
            <Brain size={12} />
          </Link>
          {canDispatch && (
            <button
              onClick={(e) => {
                e.stopPropagation();
                dispatchMutation.mutate();
              }}
              disabled={dispatchMutation.isPending}
              className="p-1 rounded opacity-0 group-hover:opacity-100 transition-opacity hover:bg-[rgba(255,255,255,0.05)] cursor-pointer touch-visible"
              title="Dispatch task"
              style={{ color: C.accent }}
            >
              <Send size={12} />
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

// ── Group header ─────────────────────────────────────────────────────────────

function GroupHeader({
  label,
  count,
  adHoc,
  progressPct,
  collapsed,
  onToggle,
  onOpenProject,
  onOpenReferences,
}: {
  label: string;
  count: number;
  adHoc?: boolean;
  progressPct?: number | null;
  collapsed: boolean;
  onToggle: () => void;
  onOpenProject?: () => void;
  onOpenReferences?: () => void;
}) {
  return (
    <div className="sticky top-0 z-[1] flex items-center gap-1.5 px-3 pt-3 pb-1" style={{ backgroundColor: C.bgBase }}>
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={!collapsed}
        className="flex items-center gap-1.5 min-w-0 cursor-pointer"
        style={{ color: C.textMuted }}
      >
        <ChevronRight
          size={11}
          className="transition-transform shrink-0"
          style={{ transform: collapsed ? "none" : "rotate(90deg)", color: C.textDim }}
        />
        {adHoc && <Zap size={10} style={{ color: C.accent }} />}
        <span className="text-[10px] font-semibold tracking-[0.08em] uppercase truncate">{label}</span>
        <span className="text-[10px] font-mono" style={{ color: C.textDim }}>
          {count}
        </span>
      </button>
      {typeof progressPct === "number" && (
        <span className="flex items-center gap-1 ml-1 shrink-0" title={`${progressPct}% done`}>
          {/* Segmented stepper (mockup pattern) — 5 blocks read faster than a hairline bar */}
          <span className="flex gap-[2px]">
            {[0, 1, 2, 3, 4].map((i) => (
              <span
                key={i}
                className="w-[9px] h-[4px] rounded-[1px]"
                style={{ backgroundColor: progressPct >= (i + 1) * 20 - 10 ? C.accent : C.bgHover }}
              />
            ))}
          </span>
          <span className="text-[9px] font-mono" style={{ color: C.textDim }}>
            {progressPct}%
          </span>
        </span>
      )}
      {(onOpenReferences || onOpenProject) && (
        <div className="ml-auto flex items-center gap-1 shrink-0">
          {onOpenReferences && (
            <button
              type="button"
              onClick={onOpenReferences}
              className="p-1 rounded cursor-pointer hover:bg-[rgba(255,255,255,0.05)] transition-colors"
              style={{ color: C.textDim }}
              title="Reference files for this project"
              aria-label={`Reference files for ${label}`}
            >
              <Paperclip size={11} />
            </button>
          )}
          {onOpenProject && (
            <button
              type="button"
              onClick={onOpenProject}
              className="text-[10px] px-1.5 py-0.5 rounded cursor-pointer hover:bg-[rgba(255,255,255,0.05)] transition-colors whitespace-nowrap"
              style={{ color: C.textMuted }}
              title="Open project view (phases)"
            >
              Phases →
            </button>
          )}
        </div>
      )}
    </div>
  );
}

// ── Column ───────────────────────────────────────────────────────────────────

export type TaskGroupMode = "status" | "project";

const DONE_PAGE = 30;

// User's view choice (group mode + collapse toggles) survives reloads.
// Search + agent filter stay ephemeral on purpose — they're filters, not a view.
const VIEW_STORAGE_KEY = "mc:tasks:view";

type StoredView = { mode?: TaskGroupMode; toggled?: string[] };

export default function TaskListColumn({
  tasks,
  projects,
  agents,
  boardId,
  selectedTaskId,
  onSelectTask,
  onOpenProject,
  focusTaskId,
  onFocusHandled,
}: {
  tasks: Task[];
  projects: Project[];
  agents: Agent[];
  boardId: string;
  selectedTaskId: string | null;
  onSelectTask: (task: Task) => void;
  onOpenProject: (projectId: string) => void;
  /** Deep-link target (e.g. from /tasks?taskId=…): expand its group + scroll to it once. */
  focusTaskId?: string | null;
  onFocusHandled?: () => void;
}) {
  // First visit (nothing stored): project view, all groups collapsed —
  // a scannable project overview instead of a wall of status lanes.
  const [mode, setMode] = useState<TaskGroupMode>("project");
  const [query, setQuery] = useState("");
  const [agentFilter, setAgentFilter] = useState<string>("");
  // Collapse state as inversion of the per-group default: done/aborted and
  // project groups start collapsed (Ad-hoc stays open), user toggles flip it.
  const [toggled, setToggled] = useState<Set<string>>(() => new Set());
  // Restore after mount (SSR has no localStorage); don't persist until restored,
  // otherwise the initial render would overwrite the stored view with defaults.
  const viewRestored = useRef(false);

  useEffect(() => {
    try {
      const raw = localStorage.getItem(VIEW_STORAGE_KEY);
      if (raw) {
        const v = JSON.parse(raw) as StoredView;
        if (v.mode === "status" || v.mode === "project") setMode(v.mode);
        if (Array.isArray(v.toggled)) {
          setToggled(new Set(v.toggled.filter((k): k is string => typeof k === "string")));
        }
      }
    } catch {
      // corrupted entry: fall back to defaults
    }
    viewRestored.current = true;
  }, []);

  useEffect(() => {
    if (!viewRestored.current) return;
    try {
      localStorage.setItem(VIEW_STORAGE_KEY, JSON.stringify({ mode, toggled: [...toggled] }));
    } catch {
      // storage full/unavailable — view just won't persist
    }
  }, [mode, toggled]);
  // Per-group render limit — 168 expanded ad-hoc rows must not land in the DOM at once
  const [limits, setLimits] = useState<Record<string, number>>({});
  // Project references dialog (ADR-053) — paperclip icon in project group headers
  const [referencesProjectId, setReferencesProjectId] = useState<string | null>(null);

  const projectName = useMemo(() => {
    const m = new Map<string, string>();
    for (const p of projects) m.set(p.id, p.name);
    return m;
  }, [projects]);

  // Top-level tasks only: subtasks live inside the project/phase view and the
  // parent task's detail — showing them flat would triple the list.
  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    return tasks.filter((t) => {
      if (t.parent_task_id) return false;
      if (agentFilter && t.assigned_agent_id !== agentFilter) return false;
      if (q && !t.title.toLowerCase().includes(q)) return false;
      return true;
    });
  }, [tasks, query, agentFilter]);

  type Group = {
    key: string;
    label: string;
    adHoc?: boolean;
    projectId?: string;
    progressPct?: number | null;
    tasks: Task[];
  };

  const groups: Group[] = useMemo(() => {
    if (mode === "status") {
      return STATUS_ORDER.map((s) => ({
        key: `status:${s}`,
        label: STATUS_LABEL[s],
        tasks: visible.filter((t) => t.status === s),
      })).filter((g) => g.tasks.length > 0);
    }
    // project mode: Ad-hoc first, then projects in list order, skip empty
    const adHoc: Group = {
      key: "proj:adhoc",
      label: "Ad-hoc",
      adHoc: true,
      tasks: visible.filter((t) => !t.project_id),
    };
    const rest: Group[] = projects.map((p) => {
      const pt = visible.filter((t) => t.project_id === p.id);
      const total = tasks.filter((t) => t.project_id === p.id && !t.parent_task_id);
      const done = total.filter((t) => t.status === "done");
      return {
        key: `proj:${p.id}`,
        label: p.name,
        projectId: p.id,
        progressPct: total.length > 0 ? Math.round((done.length / total.length) * 100) : null,
        tasks: pt,
      };
    });
    return [adHoc, ...rest].filter((g) => g.tasks.length > 0);
  }, [mode, visible, projects, tasks]);

  // Project mode: everything starts collapsed (scannable project overview);
  // status mode: only done/aborted start collapsed.
  const defaultCollapsed = (g: Group) =>
    g.key === "status:done" || g.key === "status:aborted" || !!g.projectId || !!g.adHoc;

  const toggleGroup = (key: string) =>
    setToggled((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });

  const openCount = useMemo(
    () => tasks.filter((t) => !t.parent_task_id && t.status !== "done" && t.status !== "aborted").length,
    [tasks],
  );

  // Deep-link focus: expand whichever group currently hides focusTaskId, then
  // scroll its row into view. Subtasks never appear here (visible filters
  // them out) — the effect just no-ops for those, TaskDetailBody still opens
  // via the page-level selection. One-shot per id: onFocusHandled clears the
  // prop upstream so this doesn't re-fire on every 15s task refetch.
  useEffect(() => {
    if (!focusTaskId) return;
    const group = groups.find((g) => g.tasks.some((t) => t.id === focusTaskId));
    if (!group) {
      onFocusHandled?.();
      return;
    }
    const isCollapsed = defaultCollapsed(group) !== toggled.has(group.key);
    if (isCollapsed) {
      setToggled((prev) => {
        const next = new Set(prev);
        if (next.has(group.key)) next.delete(group.key);
        else next.add(group.key);
        return next;
      });
    }
    // Paginated groups (e.g. Done) only render the first DONE_PAGE rows —
    // bump the limit so the target is actually in the DOM before scrolling.
    const index = group.tasks.findIndex((t) => t.id === focusTaskId);
    const currentLimit = limits[group.key] ?? DONE_PAGE;
    if (index >= currentLimit) {
      setLimits((m) => ({ ...m, [group.key]: index + 1 }));
    }
    const raf = requestAnimationFrame(() => {
      document.getElementById(`task-row-${focusTaskId}`)?.scrollIntoView?.({ block: "center" });
    });
    onFocusHandled?.();
    return () => cancelAnimationFrame(raf);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focusTaskId]);

  return (
    <div className="flex flex-col h-full min-h-0 w-full">
      {/* Header */}
      <div className="px-4 pt-4 pb-2 shrink-0">
        <div className="flex items-baseline gap-2">
          <h1 className="text-[15px] font-semibold" style={{ color: C.textPrimary, letterSpacing: "-0.01em" }}>
            Tasks
          </h1>
          <span className="text-[11px] font-mono" style={{ color: C.textDim }}>
            {openCount} open
          </span>
        </div>
      </div>

      {/* Controls */}
      <div className="px-3 pb-2 shrink-0 flex flex-col gap-2">
        <label className="relative block">
          <Search size={12} className="absolute left-2.5 top-1/2 -translate-y-1/2 pointer-events-none" style={{ color: C.textDim }} />
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search tasks…"
            aria-label="Search tasks"
            className="w-full rounded-lg pl-7 pr-2.5 py-1.5 text-xs outline-none transition-colors"
            style={{
              backgroundColor: C.bgDeep,
              border: `1px solid ${C.border}`,
              color: C.textPrimary,
            }}
          />
        </label>
        <div className="flex items-center gap-1.5">
          <div
            role="tablist"
            aria-label="Group tasks by"
            className="flex rounded-lg p-0.5"
            style={{ backgroundColor: C.bgDeep, border: `1px solid ${C.border}` }}
          >
            {(["status", "project"] as const).map((m) => (
              <button
                key={m}
                role="tab"
                aria-selected={mode === m}
                onClick={() => setMode(m)}
                className="px-2 py-1 rounded-md text-[10.5px] font-medium capitalize cursor-pointer transition-colors"
                style={
                  mode === m
                    ? { backgroundColor: C.accentSubtle, color: C.accentHover }
                    : { color: C.textMuted }
                }
              >
                {m === "status" ? "Status" : "Project"}
              </button>
            ))}
          </div>
          <select
            value={agentFilter}
            onChange={(e) => setAgentFilter(e.target.value)}
            aria-label="Filter by agent"
            className="text-[10.5px] rounded-lg px-2 py-1.5 outline-none cursor-pointer min-w-0 flex-1"
            style={{ backgroundColor: C.bgDeep, border: `1px solid ${C.border}`, color: agentFilter ? C.textPrimary : C.textMuted }}
          >
            <option value="">All agents</option>
            {agents.map((a) => (
              <option key={a.id} value={a.id}>
                {a.emoji ? `${a.emoji} ` : ""}
                {a.name}
              </option>
            ))}
          </select>
        </div>
      </div>

      {/* Grouped list */}
      <div className="flex-1 overflow-y-auto pb-4 px-1 min-h-0">
        {groups.length === 0 && (
          <div className="px-4 py-10 text-center text-xs" style={{ color: C.textMuted }}>
            {query || agentFilter ? "No tasks match the current filters." : "No tasks yet — create one with ⌘K or the + button."}
          </div>
        )}
        {groups.map((g) => {
          // XOR: default state per group, user toggle inverts it
          const isCollapsed = defaultCollapsed(g) !== toggled.has(g.key);
          const limit = limits[g.key] ?? DONE_PAGE;
          const shown = g.tasks.slice(0, limit);
          return (
            <section key={g.key} aria-label={g.label}>
              <GroupHeader
                label={g.label}
                count={g.tasks.length}
                adHoc={g.adHoc}
                progressPct={g.progressPct}
                collapsed={isCollapsed}
                onToggle={() => toggleGroup(g.key)}
                onOpenProject={g.projectId ? () => onOpenProject(g.projectId!) : undefined}
                onOpenReferences={g.projectId ? () => setReferencesProjectId(g.projectId!) : undefined}
              />
              {!isCollapsed && (
                <div className="px-1">
                  {shown.map((t) => (
                    <div key={t.id} id={`task-row-${t.id}`}>
                      <ListRow
                        task={t}
                        agents={agents}
                        boardId={boardId}
                        selected={t.id === selectedTaskId}
                        showProject={mode === "status"}
                        projectName={t.project_id ? (projectName.get(t.project_id) ?? null) : null}
                        onClick={() => onSelectTask(t)}
                      />
                    </div>
                  ))}
                  {g.tasks.length > limit && (
                    <button
                      type="button"
                      onClick={() => setLimits((m) => ({ ...m, [g.key]: limit + DONE_PAGE }))}
                      className="w-full text-center text-[11px] py-2 rounded-lg cursor-pointer hover:bg-[rgba(255,255,255,0.03)] transition-colors"
                      style={{ color: C.textMuted }}
                    >
                      Show {Math.min(DONE_PAGE, g.tasks.length - limit)} more
                    </button>
                  )}
                </div>
              )}
            </section>
          );
        })}
      </div>

      <ProjectReferencesDialog
        open={referencesProjectId != null}
        onClose={() => setReferencesProjectId(null)}
        projectId={referencesProjectId ?? ""}
        projectName={referencesProjectId ? (projectName.get(referencesProjectId) ?? "Project") : ""}
      />
    </div>
  );
}
