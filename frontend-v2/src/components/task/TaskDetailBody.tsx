"use client";

/**
 * TaskDetailBody — shared header + content of the task detail (07/2026 redesign).
 *
 * One body for both chromes (side panel + modal) — the previous 1:1
 * duplication is gone. Structure follows one section grammar:
 *
 *   Header      title · status dropdown · priority · agent · ⋯ menu · close
 *   Description markdown
 *   Briefing    intake fields (only when present)
 *   Properties  2×2 grid: assignee · project · created by · started
 *   Relations   parent / subtasks / depends on / report-back
 *   Checklist   progress + collapsible items
 *   Git         branch / commits / inline diff (GitPanel)
 *   Actions     run control + review (TaskActions)
 *   Tabs        Comments · Deliverables · Transcript · History
 *
 * Status changes live in the header dropdown — the old 7-chip wall is gone.
 * Delete is a two-step confirm inside the ⋯ menu (destructive ≠ prominent).
 */

import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AnimatePresence, motion } from "framer-motion";
import { Check, ChevronDown, ChevronRight, MoreHorizontal, Square, CheckSquare, AlertCircle, Trash2, X } from "lucide-react";
import { api } from "@/lib/api";
import { notify } from "@/lib/notify";
import { timeAgo } from "@/lib/utils";
import { C, LANE } from "@/lib/colors";
import { useAppStore } from "@/lib/store";
import { TaskDescription } from "./TaskDescription";
import { TaskActions } from "./TaskActions";
import { TaskComments } from "./TaskComments";
import { TaskHistory } from "./TaskHistory";
import { TaskTranscript } from "./TaskTranscript";
import { DeliverablesTab } from "./DeliverablesTab";
import { E2ETab } from "./E2ETab";
import { WorkspaceTab } from "./WorkspaceTab";
import { GitPanel } from "./GitPanel";
import { TaskReferences } from "./TaskReferences";
import type { Agent, Task, TaskChecklistItem, TaskEvent, TaskGitInfo, TaskStatus } from "@/lib/types";

// ── Status vocabulary ────────────────────────────────────────────────────────

const STATUS_LABEL: Record<TaskStatus, string> = {
  inbox: "Inbox",
  in_progress: "In Progress",
  review: "Review",
  user_test: "User Test",
  done: "Done",
  blocked: "Blocked",
  failed: "Failed",
  aborted: "Aborted",
};

const STATUS_ORDER: TaskStatus[] = [
  "inbox",
  "in_progress",
  "review",
  "user_test",
  "done",
  "blocked",
  "failed",
  "aborted",
];

// ── Small shared pieces ──────────────────────────────────────────────────────

function SectionLabel({ children, trailing }: { children: React.ReactNode; trailing?: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between mb-2">
      <span className="text-[10px] font-semibold uppercase tracking-[0.07em]" style={{ color: C.textDim }}>
        {children}
      </span>
      {trailing}
    </div>
  );
}

function Section({ children, last = false }: { children: React.ReactNode; last?: boolean }) {
  return (
    <div className="px-4 py-3" style={last ? undefined : { borderBottom: `1px solid ${C.border}` }}>
      {children}
    </div>
  );
}

/** Click-outside-closing popover shell for header menus. */
function useClickOutside(onClose: () => void) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    function handle(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose();
    }
    document.addEventListener("mousedown", handle);
    return () => document.removeEventListener("mousedown", handle);
  }, [onClose]);
  return ref;
}

// ── Status dropdown ──────────────────────────────────────────────────────────

function StatusMenu({
  status,
  onChange,
  pending,
}: {
  status: TaskStatus;
  onChange: (s: TaskStatus) => void;
  pending: boolean;
}) {
  const [open, setOpen] = useState(false);
  const ref = useClickOutside(() => setOpen(false));
  const color = LANE[status] ?? C.textMuted;

  return (
    <div className="relative" ref={ref}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        disabled={pending}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={`Status: ${STATUS_LABEL[status]} — change`}
        className="inline-flex items-center gap-1.5 rounded-md px-2 py-1 text-[11px] font-medium cursor-pointer transition-opacity hover:opacity-85"
        style={{ background: `${color}1F`, border: `1px solid ${color}55`, color }}
      >
        <span className="w-1.5 h-1.5 rounded-full" style={{ background: color }} />
        {STATUS_LABEL[status]}
        <ChevronDown size={10} style={{ color: C.textDim }} />
      </button>
      <AnimatePresence>
        {open && (
          <motion.div
            role="menu"
            initial={{ opacity: 0, y: -4 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -4 }}
            transition={{ duration: 0.12, ease: "easeOut" }}
            className="absolute left-0 top-full mt-1 z-20 min-w-[150px] rounded-lg py-1"
            style={{
              background: C.bgBase,
              border: `1px solid ${C.borderActive}`,
              boxShadow: "0 4px 24px rgba(0,0,0,0.5), 0 1px 2px rgba(0,0,0,0.3)",
            }}
          >
            {STATUS_ORDER.map((s) => {
              const c = LANE[s] ?? C.textMuted;
              const active = s === status;
              return (
                <button
                  key={s}
                  role="menuitem"
                  disabled={active}
                  onClick={() => {
                    setOpen(false);
                    onChange(s);
                  }}
                  className="w-full flex items-center gap-2 px-3 py-1.5 text-left text-xs transition-colors cursor-pointer disabled:cursor-default"
                  style={{ color: active ? C.textDim : C.textSecondary, background: active ? "rgba(255,255,255,0.03)" : "transparent" }}
                  onMouseEnter={(e) => {
                    if (!active) (e.currentTarget as HTMLElement).style.background = C.bgHover;
                  }}
                  onMouseLeave={(e) => {
                    (e.currentTarget as HTMLElement).style.background = active ? "rgba(255,255,255,0.03)" : "transparent";
                  }}
                >
                  <span className="w-1.5 h-1.5 rounded-full shrink-0" style={{ background: c }} />
                  {STATUS_LABEL[s]}
                  {active && <Check size={11} className="ml-auto" style={{ color: C.textDim }} />}
                </button>
              );
            })}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

// ── ⋯ menu (delete lives here) ───────────────────────────────────────────────

function OverflowMenu({
  isActive,
  onDelete,
  deleteLoading,
}: {
  isActive: boolean;
  onDelete: () => void;
  deleteLoading: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const ref = useClickOutside(() => {
    setOpen(false);
    setConfirm(false);
  });

  return (
    <div className="relative" ref={ref}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label="More actions"
        className="w-[30px] h-[30px] rounded-lg flex items-center justify-center transition-colors hover:bg-[rgba(255,255,255,0.05)] cursor-pointer"
        style={{ color: C.textSecondary, border: `1px solid ${C.border}` }}
      >
        <MoreHorizontal size={14} />
      </button>
      <AnimatePresence>
        {open && (
          <motion.div
            role="menu"
            initial={{ opacity: 0, y: -4 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -4 }}
            transition={{ duration: 0.12, ease: "easeOut" }}
            className="absolute right-0 top-full mt-1 z-20 min-w-[180px] rounded-lg py-1"
            style={{
              background: C.bgBase,
              border: `1px solid ${C.borderActive}`,
              boxShadow: "0 4px 24px rgba(0,0,0,0.5), 0 1px 2px rgba(0,0,0,0.3)",
            }}
          >
            {!confirm ? (
              <button
                role="menuitem"
                onClick={() => setConfirm(true)}
                className="w-full flex items-center gap-2 px-3 py-1.5 text-left text-xs transition-colors cursor-pointer"
                style={{ color: C.textSecondary }}
                onMouseEnter={(e) => ((e.currentTarget as HTMLElement).style.background = C.bgHover)}
                onMouseLeave={(e) => ((e.currentTarget as HTMLElement).style.background = "transparent")}
              >
                <Trash2 size={12} style={{ color: "#D05F5F" }} />
                Delete task
              </button>
            ) : (
              <div className="px-3 py-2 space-y-2">
                <div className="text-[11px]" style={{ color: C.textSecondary }}>
                  {isActive ? "Agent is working on this task. Delete anyway?" : "Delete this task permanently?"}
                </div>
                <div className="flex gap-1.5">
                  <button
                    onClick={onDelete}
                    disabled={deleteLoading}
                    className="px-2 py-1 rounded text-[10px] font-semibold cursor-pointer"
                    style={{ backgroundColor: `${C.error}26`, color: "#D05F5F" }}
                  >
                    {deleteLoading ? "…" : "Delete task"}
                  </button>
                  <button
                    onClick={() => {
                      setConfirm(false);
                      setOpen(false);
                    }}
                    className="px-2 py-1 rounded text-[10px] cursor-pointer"
                    style={{ color: C.textMuted }}
                  >
                    Cancel
                  </button>
                </div>
              </div>
            )}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

// ── Property cell dropdowns (assignee / project) ─────────────────────────────

function PropertyMenuCell({
  label,
  value,
  options,
  onSelect,
}: {
  label: string;
  value: string;
  options: { id: string | null; label: string; active: boolean }[];
  onSelect: (id: string | null) => void;
}) {
  const [open, setOpen] = useState(false);
  // The properties grid clips its children (overflow-hidden for the rounded
  // corners) and sits inside a scroll container — an absolute dropdown gets
  // cut off after ~2 entries. Render the menu through a portal with fixed
  // positioning measured off the trigger instead.
  const [menuPos, setMenuPos] = useState<{ top: number; bottom: number; left: number; width: number; up: boolean } | null>(null);
  const triggerRef = useRef<HTMLDivElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    function handleClick(e: MouseEvent) {
      if (
        !triggerRef.current?.contains(e.target as Node) &&
        !menuRef.current?.contains(e.target as Node)
      ) {
        setOpen(false);
      }
    }
    // Fixed positioning goes stale when the panel scrolls or resizes — close.
    function handleScroll(e: Event) {
      if (menuRef.current?.contains(e.target as Node)) return; // menu's own scrollbar
      setOpen(false);
    }
    document.addEventListener("mousedown", handleClick);
    window.addEventListener("scroll", handleScroll, true);
    window.addEventListener("resize", handleScroll);
    return () => {
      document.removeEventListener("mousedown", handleClick);
      window.removeEventListener("scroll", handleScroll, true);
      window.removeEventListener("resize", handleScroll);
    };
  }, [open]);

  const MENU_MAX = 240;
  const toggle = () => {
    if (!open && triggerRef.current) {
      const r = triggerRef.current.getBoundingClientRect();
      const up = window.innerHeight - r.bottom < MENU_MAX + 16 && r.top > MENU_MAX + 16;
      // "up" positions via bottom instead of a translate — Framer Motion
      // animates transform and would clobber a translateY(-100%).
      setMenuPos({
        top: up ? 0 : r.bottom + 4,
        bottom: up ? window.innerHeight - r.top + 4 : 0,
        left: r.left,
        width: r.width,
        up,
      });
    }
    setOpen((o) => !o);
  };

  return (
    <div className="relative" style={{ background: C.bgSurface }} ref={triggerRef}>
      <button
        type="button"
        onClick={toggle}
        aria-haspopup="listbox"
        aria-expanded={open}
        className="w-full text-left px-2.5 py-2 cursor-pointer transition-colors hover:bg-[rgba(255,255,255,0.03)]"
      >
        <span className="block text-[9px] font-semibold uppercase tracking-[0.07em] mb-0.5" style={{ color: C.textDim }}>
          {label}
        </span>
        <span className="flex items-center gap-1 text-xs truncate" style={{ color: C.textPrimary }}>
          <span className="truncate">{value}</span>
          <ChevronDown size={9} className="ml-auto shrink-0" style={{ color: C.textDim }} />
        </span>
      </button>
      {open && menuPos && createPortal(
        <AnimatePresence>
          <motion.div
            ref={menuRef}
            role="listbox"
            initial={{ opacity: 0, y: menuPos.up ? 4 : -4 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.12, ease: "easeOut" }}
            className="rounded-lg py-1 overflow-y-auto"
            style={{
              position: "fixed",
              ...(menuPos.up ? { bottom: menuPos.bottom } : { top: menuPos.top }),
              left: menuPos.left,
              width: menuPos.width,
              maxHeight: MENU_MAX,
              zIndex: 70,
              background: C.bgBase,
              border: `1px solid ${C.borderActive}`,
              boxShadow: "0 4px 24px rgba(0,0,0,0.5), 0 1px 2px rgba(0,0,0,0.3)",
            }}
          >
            {options.map((o) => (
              <button
                key={o.id ?? "__none"}
                role="option"
                aria-selected={o.active}
                onClick={() => {
                  setOpen(false);
                  if (!o.active) onSelect(o.id);
                }}
                className="w-full flex items-center gap-2 px-2.5 py-1.5 text-left text-xs transition-colors cursor-pointer"
                style={{
                  color: o.active ? C.accent : C.textSecondary,
                  background: o.active ? C.accentSubtle : "transparent",
                }}
                onMouseEnter={(e) => {
                  if (!o.active) (e.currentTarget as HTMLElement).style.background = C.bgHover;
                }}
                onMouseLeave={(e) => {
                  (e.currentTarget as HTMLElement).style.background = o.active ? C.accentSubtle : "transparent";
                }}
              >
                <span className="truncate">{o.label}</span>
                {o.active && <Check size={11} className="ml-auto shrink-0" />}
              </button>
            ))}
          </motion.div>
        </AnimatePresence>,
        document.body,
      )}
    </div>
  );
}

// ── Body ─────────────────────────────────────────────────────────────────────

const PRIORITY_COLORS: Record<string, string> = {
  critical: C.error,
  high: C.warning,
  medium: C.textSecondary,
  low: C.textMuted,
};

export function TaskDetailBody({
  task,
  agents,
  boardId,
  onClose,
}: {
  task: Task;
  agents: Agent[];
  boardId: string;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [activeTab, setActiveTab] = useState<"comments" | "history" | "transcript" | "deliverables" | "e2e" | "workspace">("comments");
  const [checklistOpen, setChecklistOpen] = useState(false);
  const [subtasksOpen, setSubtasksOpen] = useState(false);

  const agent = agents.find((a) => a.id === task.assigned_agent_id);
  const isActive = task.status === "in_progress" || task.status === "review";
  const currentUser = useAppStore((s) => s.currentUser);

  // ── Mutations ──────────────────────────────────────────────────────────────

  const updateMutation = useMutation({
    mutationFn: (data: Partial<Task>) => api.tasks.update(boardId, task.id, data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["tasks", boardId] });
      qc.invalidateQueries({ queryKey: ["pipeline", boardId] });
      qc.invalidateQueries({ queryKey: ["task", boardId, task.id] });
    },
    onError: (e: Error) => notify.error(`Update failed: ${e.message}`),
  });

  const deleteMutation = useMutation({
    mutationFn: () => api.tasks.delete(boardId, task.id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["tasks", boardId] });
      qc.invalidateQueries({ queryKey: ["pipeline", boardId] });
      onClose();
    },
    onError: (e: Error) => notify.error(`Delete failed: ${e.message}`),
  });

  // ── Queries ────────────────────────────────────────────────────────────────

  const { data: events, isLoading: isEventsLoading } = useQuery({
    queryKey: ["task-events", task.id],
    queryFn: () => api.tasks.events(boardId, task.id),
    enabled: activeTab === "history",
  });

  const { data: deliverables } = useQuery({
    queryKey: ["deliverables", boardId, task.id, "include_subtasks"],
    queryFn: () => api.tasks.deliverables.list(boardId, task.id, { includeSubtasks: true, depth: 2 }),
    enabled: activeTab === "deliverables",
  });

  // Shared query key with TaskComments — cache hit there, only fetched here
  // to decide whether the E2E tab should show up for tasks that weren't
  // flagged `e2e_test_required` but still received a test result comment.
  const { data: comments } = useQuery({
    queryKey: ["task-comments", task.id],
    queryFn: () => api.tasks.comments.list(boardId, task.id),
  });
  const hasE2EResult = (comments ?? []).some((c) => /\*\*Result:\*\*\s*TEST_(PASS|FAIL)/.test(c.content));

  const { data: gitInfo } = useQuery<TaskGitInfo>({
    queryKey: ["task-git-info", boardId, task.id],
    queryFn: () => api.tasks.gitInfo(boardId, task.id),
    enabled: !!task.workspace_path,
    refetchInterval: 30_000,
  });

  const { data: checklist = [] } = useQuery<TaskChecklistItem[]>({
    queryKey: ["task-checklist", boardId, task.id],
    queryFn: () => api.tasks.checklist.list(boardId, task.id),
    refetchInterval: 15_000,
  });

  const { data: hierarchy } = useQuery({
    queryKey: ["task-hierarchy", boardId, task.id],
    queryFn: () => api.tasks.hierarchy(boardId, task.id),
  });

  const { data: dependencies } = useQuery({
    queryKey: ["task-dependencies", task.id],
    queryFn: () => api.tasks.dependencies(boardId, task.id),
  });

  const { data: projects = [] } = useQuery({
    queryKey: ["projects", boardId],
    queryFn: () => api.projects.list(boardId),
    enabled: !!boardId,
  });

  const { data: usersList } = useQuery({
    queryKey: ["users-list"],
    queryFn: () => api.auth.users.list(),
    enabled: !!task.created_by_user_id && task.created_by_user_id !== currentUser?.id,
    staleTime: 60_000,
  });
  const creatorName = task.created_by_user_id
    ? task.created_by_user_id === currentUser?.id
      ? currentUser.name
      : (usersList?.find((u) => u.id === task.created_by_user_id)?.name ?? "User")
    : null;

  // ── Briefing fields ────────────────────────────────────────────────────────

  const briefingFields: { label: string; value: string | null | undefined }[] = task.intake_mode
    ? [
        { label: "Type", value: task.request_kind },
        { label: "Output", value: task.desired_output },
        { label: "Out of scope", value: task.scope_out },
        { label: "Risks", value: task.risk_notes },
        { label: "Criteria", value: task.acceptance_criteria },
        { label: "Browser", value: task.needs_browser ? "Yes" : null },
        { label: "E2E test", value: task.e2e_test_required ? "Required" : null },
        { label: "Credentials", value: task.requires_auth ? "Yes" : null },
        { label: "Approval", value: task.approval_policy },
        { label: "Autonomy", value: task.autonomy_level },
        { label: "Links", value: task.reference_urls?.join(", ") || null },
        { label: "Notes", value: task.reference_notes },
      ].filter((f) => f.value)
    : [];

  const checklistDone = checklist.filter((i) => i.status === "done").length;
  const projectName = task.project_id ? (projects.find((p) => p.id === task.project_id)?.name ?? "Project") : "Ad-hoc";

  const tabs: { key: typeof activeTab; label: string }[] = [
    { key: "comments", label: "Comments" },
    { key: "deliverables", label: "Deliverables" },
    ...(task.workspace_path ? [{ key: "workspace" as const, label: "Workspace" }] : []),
    ...(task.e2e_test_required || hasE2EResult ? [{ key: "e2e" as const, label: "E2E" }] : []),
    ...(task.spawn_session_key || task.dispatched_at ? [{ key: "transcript" as const, label: "Transcript" }] : []),
    { key: "history", label: "History" },
  ];

  return (
    <>
      {/* ── Header ── */}
      <div className="px-4 pt-4 pb-3 shrink-0" style={{ borderBottom: `1px solid ${C.border}` }}>
        <div className="flex items-start gap-3">
          <h2 className="flex-1 min-w-0 text-[15px] font-semibold leading-snug" style={{ color: C.textPrimary }}>
            {task.title}
          </h2>
          <div className="flex items-center gap-1.5 shrink-0">
            <OverflowMenu isActive={isActive} onDelete={() => deleteMutation.mutate()} deleteLoading={deleteMutation.isPending} />
            <button
              onClick={onClose}
              aria-label="Close task details"
              className="w-[30px] h-[30px] rounded-lg flex items-center justify-center transition-colors hover:bg-[rgba(255,255,255,0.05)] cursor-pointer"
              style={{ color: C.textSecondary, border: `1px solid ${C.border}` }}
            >
              <X size={15} />
            </button>
          </div>
        </div>
        <div className="flex items-center gap-1.5 mt-2.5 flex-wrap">
          <StatusMenu
            status={task.status}
            pending={updateMutation.isPending}
            onChange={(s) => updateMutation.mutate({ status: s } as Partial<Task>)}
          />
          <span
            className="inline-flex items-center rounded-md px-2 py-1 text-[11px] capitalize"
            style={{ color: PRIORITY_COLORS[task.priority] ?? C.textMuted, border: `1px solid ${C.border}` }}
          >
            {task.priority}
          </span>
          {agent && (
            <span
              className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-[11px]"
              style={{ color: C.textSecondary, border: `1px solid ${C.border}` }}
            >
              {agent.emoji} {agent.name}
            </span>
          )}
        </div>
      </div>

      {/* ── Scrollable body ── */}
      <div
        className="flex-1 overflow-y-auto"
        style={{ overscrollBehavior: "contain", WebkitOverflowScrolling: "touch" } as React.CSSProperties}
      >
        {/* Description */}
        {task.description && (
          <Section>
            <SectionLabel>Description</SectionLabel>
            <TaskDescription description={task.description} />
          </Section>
        )}

        {/* Briefing */}
        {briefingFields.length > 0 && (
          <Section>
            <SectionLabel>Briefing · {task.intake_mode}</SectionLabel>
            <div className="space-y-1">
              {briefingFields.map((f) => (
                <div key={f.label} className="text-xs">
                  <span style={{ color: C.textMuted }}>{f.label}: </span>
                  <span style={{ color: C.textPrimary }}>{f.value}</span>
                </div>
              ))}
            </div>
          </Section>
        )}

        {/* Properties */}
        <Section>
          <SectionLabel>Properties</SectionLabel>
          <div
            className="grid grid-cols-2 gap-px rounded-lg overflow-hidden"
            style={{ background: C.border, border: `1px solid ${C.border}` }}
          >
            <PropertyMenuCell
              label="Assignee"
              value={agent ? `${agent.emoji ?? ""} ${agent.name}`.trim() : "Unassigned"}
              options={agents.map((a) => ({
                id: a.id,
                label: `${a.emoji ?? ""} ${a.name}`.trim(),
                active: a.id === task.assigned_agent_id,
              }))}
              onSelect={(id) => id && updateMutation.mutate({ assigned_agent_id: id } as Partial<Task>)}
            />
            <PropertyMenuCell
              label="Project"
              value={projectName}
              options={[
                { id: null, label: "Ad-hoc (no project)", active: !task.project_id },
                ...projects.map((p) => ({ id: p.id, label: p.name, active: p.id === task.project_id })),
              ]}
              onSelect={(id) => updateMutation.mutate({ project_id: id } as Partial<Task>)}
            />
            <div className="px-2.5 py-2" style={{ background: C.bgSurface }}>
              <span className="block text-[9px] font-semibold uppercase tracking-[0.07em] mb-0.5" style={{ color: C.textDim }}>
                Created by
              </span>
              <span className="text-xs" style={{ color: C.textPrimary }}>
                {creatorName ?? "—"} · {timeAgo(task.created_at)}
              </span>
            </div>
            <div className="px-2.5 py-2" style={{ background: C.bgSurface }}>
              <span className="block text-[9px] font-semibold uppercase tracking-[0.07em] mb-0.5" style={{ color: C.textDim }}>
                Started
              </span>
              <span className="text-xs" style={{ color: C.textPrimary }}>
                {task.started_at ? timeAgo(task.started_at) : "—"}
              </span>
            </div>
          </div>
        </Section>

        {/* Relations */}
        {(hierarchy?.parent || (hierarchy?.children?.length ?? 0) > 0 || (dependencies?.length ?? 0) > 0) && (
          <Section>
            <SectionLabel>Relations</SectionLabel>
            <div className="space-y-2">
              {hierarchy?.parent && (
                <div className="flex items-center gap-2 text-xs">
                  <span className="shrink-0" style={{ color: C.textMuted }}>
                    Parent
                  </span>
                  <span className="w-1.5 h-1.5 rounded-full shrink-0" style={{ background: LANE[hierarchy.parent.status] ?? C.textMuted }} />
                  <span className="truncate" style={{ color: C.textSecondary }} title={hierarchy.parent.title}>
                    {hierarchy.parent.title}
                  </span>
                </div>
              )}
              {(hierarchy?.children?.length ?? 0) > 0 && (
                <div>
                  <button
                    onClick={() => setSubtasksOpen((o) => !o)}
                    aria-expanded={subtasksOpen}
                    className="flex items-center gap-2 w-full text-left cursor-pointer text-xs"
                    style={{ color: C.textMuted }}
                  >
                    <span>Subtasks</span>
                    <span className="font-mono text-[10px]" style={{ color: C.textDim }}>
                      {hierarchy!.children.filter((c: { status: string }) => c.status === "done").length}/{hierarchy!.children.length} done
                    </span>
                    <ChevronRight
                      size={10}
                      className="transition-transform ml-auto"
                      style={{ transform: subtasksOpen ? "rotate(90deg)" : "none", color: C.textDim }}
                    />
                  </button>
                  <AnimatePresence>
                    {subtasksOpen && (
                      <motion.div
                        initial={{ height: 0, opacity: 0 }}
                        animate={{ height: "auto", opacity: 1 }}
                        exit={{ height: 0, opacity: 0 }}
                        transition={{ duration: 0.15 }}
                        className="overflow-hidden"
                      >
                        <div className="mt-1.5 pl-1 space-y-1">
                          {hierarchy!.children.map((c: { id: string; title: string; status: string }) => (
                            <div key={c.id} className="flex items-center gap-1.5">
                              <span className="w-2 h-2 rounded-full shrink-0" style={{ backgroundColor: LANE[c.status] ?? C.textMuted }} />
                              <span className="text-xs truncate" style={{ color: C.textSecondary }} title={c.title}>
                                {c.title}
                              </span>
                            </div>
                          ))}
                        </div>
                      </motion.div>
                    )}
                  </AnimatePresence>
                </div>
              )}
              {(dependencies?.length ?? 0) > 0 && (
                <div>
                  <div className="text-xs mb-1" style={{ color: C.textMuted }}>
                    Depends on
                  </div>
                  <div className="flex flex-col gap-1">
                    {dependencies!.map((dep) => (
                      <div key={dep.task_id} className="flex items-center gap-2 text-xs">
                        <span
                          className="w-2 h-2 rounded-full shrink-0"
                          style={{ backgroundColor: dep.status === "done" ? C.online : C.textMuted }}
                        />
                        <span style={{ color: dep.status === "done" ? C.textMuted : C.textPrimary }}>{dep.title}</span>
                        <span style={{ color: C.textMuted }}>({dep.status.replace("_", " ")})</span>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          </Section>
        )}

        {/* Checklist */}
        {checklist.length > 0 && (
          <Section>
            <button
              onClick={() => setChecklistOpen((o) => !o)}
              aria-expanded={checklistOpen}
              className="w-full flex items-center gap-2 cursor-pointer"
            >
              <span className="text-[10px] font-semibold uppercase tracking-[0.07em]" style={{ color: C.textDim }}>
                Checklist
              </span>
              <span className="flex-1 max-w-[96px] h-[3px] rounded-full overflow-hidden" style={{ backgroundColor: C.bgHover }}>
                <span
                  className="block h-full transition-all"
                  style={{
                    width: `${checklist.length ? (checklistDone / checklist.length) * 100 : 0}%`,
                    backgroundColor: C.accent,
                  }}
                />
              </span>
              <span className="text-[10px] font-mono" style={{ color: C.textDim }}>
                {checklistDone}/{checklist.length}
              </span>
              <ChevronRight
                size={10}
                className="transition-transform ml-auto"
                style={{ transform: checklistOpen ? "rotate(90deg)" : "none", color: C.textDim }}
              />
            </button>
            <AnimatePresence>
              {checklistOpen && (
                <motion.div
                  initial={{ height: 0, opacity: 0 }}
                  animate={{ height: "auto", opacity: 1 }}
                  exit={{ height: 0, opacity: 0 }}
                  transition={{ duration: 0.15 }}
                  className="overflow-hidden"
                >
                  <div className="mt-2 space-y-1">
                    {checklist.map((item) => (
                      <div key={item.id} className="flex items-center gap-2 text-xs">
                        {item.status === "done" ? (
                          <CheckSquare size={12} style={{ color: C.online, flexShrink: 0 }} />
                        ) : item.status === "blocked" ? (
                          <AlertCircle size={12} style={{ color: C.error, flexShrink: 0 }} />
                        ) : (
                          <Square size={12} style={{ color: C.textMuted, flexShrink: 0 }} />
                        )}
                        <span
                          style={{
                            color: item.status === "done" ? C.textMuted : C.textPrimary,
                            textDecoration: item.status === "done" ? "line-through" : "none",
                          }}
                        >
                          {item.title}
                        </span>
                      </div>
                    ))}
                  </div>
                </motion.div>
              )}
            </AnimatePresence>
          </Section>
        )}

        {/* References (ADR-053) */}
        <Section>
          <SectionLabel>References</SectionLabel>
          <TaskReferences taskId={task.id} />
        </Section>

        {/* Git */}
        {gitInfo?.branch && (
          <Section>
            <SectionLabel>Git</SectionLabel>
            <GitPanel gitInfo={gitInfo} boardId={boardId} taskId={task.id} />
          </Section>
        )}

        {/* Actions (run control, review) */}
        <Section>
          <TaskActions task={task} boardId={boardId} />
        </Section>

        {/* Tabs */}
        <div className="flex gap-0.5 px-4" style={{ borderBottom: `1px solid ${C.border}` }} role="tablist">
          {tabs.map((tab) => {
            const active = activeTab === tab.key;
            return (
              <button
                key={tab.key}
                role="tab"
                aria-selected={active}
                onClick={() => setActiveTab(tab.key)}
                className="px-2.5 py-2 text-[11.5px] cursor-pointer transition-colors -mb-px"
                style={{
                  color: active ? C.textPrimary : C.textMuted,
                  fontWeight: active ? 500 : 400,
                  borderBottom: `2px solid ${active ? C.accent : "transparent"}`,
                }}
              >
                {tab.label}
              </button>
            );
          })}
        </div>
        <div className="px-4 py-3 pb-4">
          {activeTab === "comments" ? (
            <TaskComments task={task} boardId={boardId} agents={agents} />
          ) : activeTab === "transcript" ? (
            <TaskTranscript taskId={task.id} isLive={task.status === "in_progress" || task.status === "review"} />
          ) : activeTab === "deliverables" ? (
            <DeliverablesTab deliverables={deliverables ?? []} boardId={boardId} taskId={task.id} />
          ) : activeTab === "workspace" ? (
            <WorkspaceTab task={task} boardId={boardId} />
          ) : activeTab === "e2e" ? (
            <E2ETab task={task} boardId={boardId} />
          ) : (
            <TaskHistory events={(events as TaskEvent[]) ?? []} isLoading={isEventsLoading} />
          )}
        </div>
      </div>
    </>
  );
}
