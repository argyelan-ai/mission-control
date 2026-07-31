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
import { useLocale, useTranslations } from "next-intl";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AnimatePresence, motion } from "framer-motion";
import { Check, ChevronDown, ChevronRight, MoreHorizontal, Square, CheckSquare, AlertCircle, Trash2, X } from "lucide-react";
import { api } from "@/lib/api";
import { notify } from "@/lib/notify";
import { timeAgo } from "@/lib/utils";
import { C, LANE, STATUS_TEXT } from "@/lib/colors";
import { useAppStore } from "@/lib/store";
import { TaskDescription } from "./TaskDescription";
import { TaskActions } from "./TaskActions";
import { TaskComments } from "./TaskComments";
import { TaskHistory } from "./TaskHistory";
import { TaskTimeline } from "./TaskTimeline";
import { TaskTranscript } from "./TaskTranscript";
import { DeliverablesTab } from "./DeliverablesTab";
import { E2ETab } from "./E2ETab";
import { WorkspaceTab } from "./WorkspaceTab";
import { ThreadPanel } from "./ThreadPanel";
import { GitPanel } from "./GitPanel";
import { TaskReferences } from "./TaskReferences";
import type { Agent, Task, TaskChecklistItem, TaskEvent, TaskGitInfo, TaskStatus } from "@/lib/types";

// ── Status vocabulary ────────────────────────────────────────────────────────

// Message keys in the tasks.* namespace — t() at the render site.
const STATUS_LABEL_KEY: Record<TaskStatus, string> = {
  inbox: "statusInbox",
  in_progress: "statusInProgress",
  review: "statusReview",
  user_test: "statusUserTest",
  waiting: "statusWaiting",
  done: "statusDone",
  blocked: "statusBlocked",
  failed: "statusFailed",
  aborted: "statusAborted",
};

const STATUS_ORDER: TaskStatus[] = [
  "inbox",
  "in_progress",
  "waiting",
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

/** Fixed-position coordinates for a body-portaled dropdown. */
type PortalMenuPos = { top: number; bottom: number; left: number; width?: number; up: boolean };

/**
 * Portal-menu plumbing shared by the header/property dropdowns: measures the
 * trigger on open and returns fixed coordinates for a menu rendered through
 * `createPortal`, closing on outside click / Escape / scroll / resize.
 * Horizontal position is clamped so the menu — including its right edge —
 * stays inside the viewport with an 8px margin (matters at 393px). `width`
 * is the menu width in px (or "trigger" to match the trigger), `align: right`
 * anchors the menu's right edge to the trigger's right edge, and `flipMax`
 * (menu max height) makes it open upward when there is no room below.
 */
function usePortalMenu({
  width,
  align = "left",
  flipMax,
}: {
  width: number | "trigger";
  align?: "left" | "right";
  flipMax?: number;
}) {
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState<PortalMenuPos | null>(null);
  const triggerRef = useRef<HTMLDivElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const close = () => setOpen(false);
    function handleClick(e: MouseEvent) {
      if (
        !triggerRef.current?.contains(e.target as Node) &&
        !menuRef.current?.contains(e.target as Node)
      ) {
        close();
      }
    }
    function handleKey(e: KeyboardEvent) {
      if (e.key === "Escape") close();
    }
    // Fixed positioning goes stale when the panel scrolls or resizes — close.
    function handleScroll(e: Event) {
      if (menuRef.current?.contains(e.target as Node)) return; // menu's own scrollbar
      close();
    }
    document.addEventListener("mousedown", handleClick);
    document.addEventListener("keydown", handleKey);
    window.addEventListener("scroll", handleScroll, true);
    window.addEventListener("resize", handleScroll);
    return () => {
      document.removeEventListener("mousedown", handleClick);
      document.removeEventListener("keydown", handleKey);
      window.removeEventListener("scroll", handleScroll, true);
      window.removeEventListener("resize", handleScroll);
    };
  }, [open]);

  const toggle = () => {
    if (!open && triggerRef.current) {
      const r = triggerRef.current.getBoundingClientRect();
      const menuWidth = width === "trigger" ? r.width : width;
      const up = flipMax != null && window.innerHeight - r.bottom < flipMax + 16 && r.top > flipMax + 16;
      // "up" positions via bottom instead of a translate — Framer Motion
      // animates transform and would clobber a translateY(-100%).
      const desiredLeft = align === "right" ? r.right - menuWidth : r.left;
      // Keep the menu (incl. right edge) inside the viewport, 8px margin.
      const left = Math.min(Math.max(desiredLeft, 8), Math.max(8, window.innerWidth - menuWidth - 8));
      setPos({
        top: up ? 0 : r.bottom + 4,
        bottom: up ? window.innerHeight - r.top + 4 : 0,
        left,
        width: width === "trigger" ? r.width : undefined,
        up,
      });
    }
    setOpen((o) => !o);
  };

  return { open, setOpen, toggle, pos, triggerRef, menuRef };
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
  const t = useTranslations("tasks");
  const color = LANE[status] ?? C.textMuted;
  // Portaled with fixed positioning + viewport clamp (see usePortalMenu) so
  // the menu can never run off the right edge on narrow (393px) viewports.
  const { open, setOpen, toggle, pos, triggerRef, menuRef } = usePortalMenu({ width: 150 });

  return (
    <div className="relative" ref={triggerRef}>
      <button
        type="button"
        onClick={toggle}
        disabled={pending}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={t("statusChange", { label: t(STATUS_LABEL_KEY[status]) })}
        className="inline-flex items-center gap-1.5 rounded-md px-2 py-1 text-[11px] font-medium cursor-pointer transition-opacity hover:opacity-85"
        style={{ background: `${color}1F`, border: `1px solid ${color}55`, color }}
      >
        <span className="w-1.5 h-1.5 rounded-full" style={{ background: color }} />
        {t(STATUS_LABEL_KEY[status])}
        <ChevronDown size={10} style={{ color: C.textDim }} />
      </button>
      {open && pos && createPortal(
        <AnimatePresence>
          <motion.div
            ref={menuRef}
            role="menu"
            initial={{ opacity: 0, y: -4 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -4 }}
            transition={{ duration: 0.12, ease: "easeOut" }}
            className="min-w-[150px] rounded-md py-1"
            style={{
              position: "fixed",
              top: pos.top,
              left: pos.left,
              zIndex: 70,
              background: C.bgBase,
              border: `1px solid ${C.borderActive}`,
              boxShadow: "var(--shadow-elevated)",
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
                  style={{ color: active ? C.textDim : C.textSecondary, background: active ? C.bgElevated : "transparent" }}
                  onMouseEnter={(e) => {
                    if (!active) (e.currentTarget as HTMLElement).style.background = C.bgHover;
                  }}
                  onMouseLeave={(e) => {
                    (e.currentTarget as HTMLElement).style.background = active ? C.bgElevated : "transparent";
                  }}
                >
                  <span className="w-1.5 h-1.5 rounded-full shrink-0" style={{ background: c }} />
                  {t(STATUS_LABEL_KEY[s])}
                  {active && <Check size={11} className="ml-auto" style={{ color: C.textDim }} />}
                </button>
              );
            })}
          </motion.div>
        </AnimatePresence>,
        document.body,
      )}
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
  const t = useTranslations("tasks");
  const [confirm, setConfirm] = useState(false);
  // Portaled with fixed positioning + viewport clamp (see usePortalMenu);
  // right-aligned to the trigger like the old `right-0` dropdown.
  const { open, setOpen, toggle, pos, triggerRef, menuRef } = usePortalMenu({ width: 180, align: "right" });
  // Closing the menu always resets the two-step delete confirm.
  useEffect(() => {
    if (!open) setConfirm(false);
  }, [open]);

  return (
    <div className="relative" ref={triggerRef}>
      <button
        type="button"
        onClick={toggle}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={t("moreActions")}
        className="w-[30px] h-[30px] rounded-md flex items-center justify-center transition-colors hover:bg-[var(--color-bg-hover)] cursor-pointer"
        style={{ color: C.textSecondary, border: `1px solid ${C.border}` }}
      >
        <MoreHorizontal size={14} />
      </button>
      {open && pos && createPortal(
        <AnimatePresence>
          <motion.div
            ref={menuRef}
            role="menu"
            initial={{ opacity: 0, y: -4 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -4 }}
            transition={{ duration: 0.12, ease: "easeOut" }}
            className="min-w-[180px] rounded-md py-1"
            style={{
              position: "fixed",
              top: pos.top,
              left: pos.left,
              zIndex: 70,
              background: C.bgBase,
              border: `1px solid ${C.borderActive}`,
              boxShadow: "var(--shadow-elevated)",
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
                <Trash2 size={12} style={{ color: STATUS_TEXT.error }} />
                {t("deleteTask")}
              </button>
            ) : (
              <div className="px-3 py-2 space-y-2">
                <div className="text-[11px]" style={{ color: C.textSecondary }}>
                  {isActive ? t("deleteWhileActive") : t("deleteConfirm")}
                </div>
                <div className="flex gap-1.5">
                  <button
                    onClick={onDelete}
                    disabled={deleteLoading}
                    className="px-2 py-1 rounded text-[10px] font-semibold cursor-pointer"
                    style={{ backgroundColor: `${C.error}26`, color: STATUS_TEXT.error }}
                  >
                    {deleteLoading ? "…" : t("deleteTask")}
                  </button>
                  <button
                    onClick={() => {
                      setConfirm(false);
                      setOpen(false);
                    }}
                    className="px-2 py-1 rounded text-[10px] cursor-pointer"
                    style={{ color: C.textMuted }}
                  >
                    {t("cancel")}
                  </button>
                </div>
              </div>
            )}
          </motion.div>
        </AnimatePresence>,
        document.body,
      )}
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
  // The properties grid clips its children (overflow-hidden for the rounded
  // corners) and sits inside a scroll container — an absolute dropdown gets
  // cut off after ~2 entries. Render the menu through a portal with fixed
  // positioning measured off the trigger instead; usePortalMenu also clamps
  // the horizontal position so the menu stays inside the viewport.
  const MENU_MAX = 240;
  const { open, setOpen, toggle, pos, triggerRef, menuRef } = usePortalMenu({ width: "trigger", flipMax: MENU_MAX });

  return (
    <div className="relative" style={{ background: C.bgSurface }} ref={triggerRef}>
      <button
        type="button"
        onClick={toggle}
        aria-haspopup="listbox"
        aria-expanded={open}
        className="w-full text-left px-2.5 py-2 cursor-pointer transition-colors hover:bg-[var(--color-bg-hover)]"
      >
        <span className="block text-[9px] font-semibold uppercase tracking-[0.07em] mb-0.5" style={{ color: C.textDim }}>
          {label}
        </span>
        <span className="flex items-center gap-1 text-xs truncate" style={{ color: C.textPrimary }}>
          <span className="truncate">{value}</span>
          <ChevronDown size={9} className="ml-auto shrink-0" style={{ color: C.textDim }} />
        </span>
      </button>
      {open && pos && createPortal(
        <AnimatePresence>
          <motion.div
            ref={menuRef}
            role="listbox"
            initial={{ opacity: 0, y: pos.up ? 4 : -4 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.12, ease: "easeOut" }}
            className="rounded-lg py-1 overflow-y-auto"
            style={{
              position: "fixed",
              ...(pos.up ? { bottom: pos.bottom } : { top: pos.top }),
              left: pos.left,
              width: pos.width,
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
  const t = useTranslations("tasks");
  const locale = useLocale();
  const qc = useQueryClient();
  const [activeTab, setActiveTab] = useState<"thread" | "comments" | "timeline" | "history" | "transcript" | "deliverables" | "e2e" | "workspace">("thread");
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
    onError: (e: Error) => notify.error(t("updateFailed", { msg: e.message })),
  });

  const deleteMutation = useMutation({
    mutationFn: () => api.tasks.delete(boardId, task.id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["tasks", boardId] });
      qc.invalidateQueries({ queryKey: ["pipeline", boardId] });
      onClose();
    },
    onError: (e: Error) => notify.error(t("deleteFailed", { msg: e.message })),
  });

  // ── Queries ────────────────────────────────────────────────────────────────

  const { data: events, isLoading: isEventsLoading } = useQuery({
    queryKey: ["task-events", task.id],
    queryFn: () => api.tasks.events(boardId, task.id),
    enabled: activeTab === "history",
  });

  const { data: timeline, isLoading: isTimelineLoading } = useQuery({
    queryKey: ["task-timeline", boardId, task.id],
    queryFn: () => api.tasks.timeline(boardId, task.id),
    enabled: activeTab === "timeline",
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
      : (usersList?.find((u) => u.id === task.created_by_user_id)?.name ?? t("userFallback"))
    : null;

  // ── Briefing fields ────────────────────────────────────────────────────────

  const briefingFields: { label: string; value: string | null | undefined }[] = task.intake_mode
    ? [
        { label: t("briefType"), value: task.request_kind },
        { label: t("briefOutput"), value: task.desired_output },
        { label: t("briefOutOfScope"), value: task.scope_out },
        { label: t("briefRisks"), value: task.risk_notes },
        { label: t("briefCriteria"), value: task.acceptance_criteria },
        { label: t("briefBrowser"), value: task.needs_browser ? t("yes") : null },
        { label: t("briefE2E"), value: task.e2e_test_required ? t("required") : null },
        { label: t("briefCredentials"), value: task.requires_auth ? t("yes") : null },
        { label: t("briefApproval"), value: task.approval_policy },
        { label: t("briefAutonomy"), value: task.autonomy_level },
        { label: t("briefLinks"), value: task.reference_urls?.join(", ") || null },
        { label: t("briefNotes"), value: task.reference_notes },
      ].filter((f) => f.value)
    : [];

  const checklistDone = checklist.filter((i) => i.status === "done").length;
  const projectName = task.project_id ? (projects.find((p) => p.id === task.project_id)?.name ?? t("projectFallback")) : t("adHoc");

  const tabs: { key: typeof activeTab; label: string }[] = [
    { key: "thread", label: t("tabThread") },
    { key: "comments", label: t("tabComments") },
    { key: "deliverables", label: t("tabDeliverables") },
    ...(task.workspace_path ? [{ key: "workspace" as const, label: t("tabWorkspace") }] : []),
    ...(task.e2e_test_required || hasE2EResult ? [{ key: "e2e" as const, label: t("tabE2E") }] : []),
    ...(task.spawn_session_key || task.dispatched_at ? [{ key: "transcript" as const, label: t("tabTranscript") }] : []),
    { key: "timeline", label: t("tabTimeline") },
    { key: "history", label: t("tabHistory") },
  ];

  return (
    <>
      {/* ── Header ── */}
      <div className="px-4 pt-4 pb-3 shrink-0" style={{ borderBottom: `1px solid ${C.border}` }}>
        <div className="label-sys label-sys--dim mb-1.5">{t("taskLabel")} · {task.id.slice(0, 8)}</div>
        <div className="flex items-start gap-3">
          <h2 className="flex-1 min-w-0 text-[15px] font-semibold leading-snug" style={{ color: C.textPrimary }}>
            {task.title}
          </h2>
          <div className="flex items-center gap-1.5 shrink-0">
            <OverflowMenu isActive={isActive} onDelete={() => deleteMutation.mutate()} deleteLoading={deleteMutation.isPending} />
            <button
              onClick={onClose}
              aria-label={t("closeTaskDetails")}
              className="w-[30px] h-[30px] rounded-md flex items-center justify-center transition-colors hover:bg-[var(--color-bg-hover)] cursor-pointer"
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
            <SectionLabel>{t("description")}</SectionLabel>
            <TaskDescription description={task.description} />
          </Section>
        )}

        {/* Briefing */}
        {briefingFields.length > 0 && (
          <Section>
            <SectionLabel>{t("briefing")} · {task.intake_mode}</SectionLabel>
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
          <SectionLabel>{t("properties")}</SectionLabel>
          <div
            className="grid grid-cols-2 gap-px rounded-lg overflow-hidden"
            style={{ background: C.border, border: `1px solid ${C.border}` }}
          >
            <PropertyMenuCell
              label={t("assignee")}
              value={agent ? `${agent.emoji ?? ""} ${agent.name}`.trim() : t("unassigned")}
              options={agents.map((a) => ({
                id: a.id,
                label: `${a.emoji ?? ""} ${a.name}`.trim(),
                active: a.id === task.assigned_agent_id,
              }))}
              onSelect={(id) => id && updateMutation.mutate({ assigned_agent_id: id } as Partial<Task>)}
            />
            <PropertyMenuCell
              label={t("projectFallback")}
              value={projectName}
              options={[
                { id: null, label: t("adHocNoProject"), active: !task.project_id },
                ...projects.map((p) => ({ id: p.id, label: p.name, active: p.id === task.project_id })),
              ]}
              onSelect={(id) => updateMutation.mutate({ project_id: id } as Partial<Task>)}
            />
            <div className="px-2.5 py-2" style={{ background: C.bgSurface }}>
              <span className="block text-[9px] font-semibold uppercase tracking-[0.07em] mb-0.5" style={{ color: C.textDim }}>
                {t("createdBy")}
              </span>
              <span className="text-xs" style={{ color: C.textPrimary }}>
                {creatorName ?? "—"} · {timeAgo(task.created_at, locale)}
              </span>
            </div>
            <div className="px-2.5 py-2" style={{ background: C.bgSurface }}>
              <span className="block text-[9px] font-semibold uppercase tracking-[0.07em] mb-0.5" style={{ color: C.textDim }}>
                {t("started")}
              </span>
              <span className="text-xs" style={{ color: C.textPrimary }}>
                {task.started_at ? timeAgo(task.started_at, locale) : "—"}
              </span>
            </div>
          </div>
        </Section>

        {/* Relations */}
        {(hierarchy?.parent || (hierarchy?.children?.length ?? 0) > 0 || (dependencies?.length ?? 0) > 0) && (
          <Section>
            <SectionLabel>{t("relations")}</SectionLabel>
            <div className="space-y-2">
              {hierarchy?.parent && (
                <div className="flex items-center gap-2 text-xs">
                  <span className="shrink-0" style={{ color: C.textMuted }}>
                    {t("parent")}
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
                    <span>{t("subtasks")}</span>
                    <span className="font-mono text-[10px]" style={{ color: C.textDim }}>
                      {t("doneOfTotal", { done: hierarchy!.children.filter((c: { status: string }) => c.status === "done").length, total: hierarchy!.children.length })}
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
                    {t("dependsOn")}
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
                {t("checklist")}
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
          <SectionLabel>{t("references")}</SectionLabel>
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

        {/* Tabs — v3: Mono-Labels, eckiger Akzent-Unterstrich für den aktiven Tab */}
        <div className="flex gap-0.5 px-4 tab-strip" style={{ borderBottom: `1px solid ${C.border}` }} role="tablist">
          {tabs.map((tab) => {
            const active = activeTab === tab.key;
            return (
              <button
                key={tab.key}
                role="tab"
                aria-selected={active}
                onClick={() => setActiveTab(tab.key)}
                className="px-2.5 py-2 font-mono text-[10px] uppercase tracking-[0.12em] cursor-pointer transition-colors -mb-px"
                style={{
                  color: active ? C.accent : C.textMuted,
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
          {activeTab === "thread" ? (
            <ThreadPanel taskId={task.id} />
          ) : activeTab === "comments" ? (
            <TaskComments task={task} boardId={boardId} agents={agents} />
          ) : activeTab === "transcript" ? (
            <TaskTranscript taskId={task.id} isLive={task.status === "in_progress" || task.status === "review"} />
          ) : activeTab === "deliverables" ? (
            <DeliverablesTab deliverables={deliverables ?? []} boardId={boardId} taskId={task.id} />
          ) : activeTab === "workspace" ? (
            <WorkspaceTab task={task} boardId={boardId} />
          ) : activeTab === "e2e" ? (
            <E2ETab task={task} boardId={boardId} />
          ) : activeTab === "timeline" ? (
            <TaskTimeline
              entries={timeline?.entries ?? []}
              isLoading={isTimelineLoading}
              truncated={timeline?.truncated}
            />
          ) : (
            <TaskHistory events={(events as TaskEvent[]) ?? []} isLoading={isEventsLoading} />
          )}
        </div>
      </div>
    </>
  );
}
