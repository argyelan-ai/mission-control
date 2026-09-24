"use client";

/**
 * TaskDetailBody — shared content of the task detail (wave 3a "task detail
 * lite", 09/2026). One body for three chromes: the /tasks split (~800 px),
 * the Home modal (~672 px) and the phone (390 px) — the layout follows the
 * width of the CONTAINER (CSS container queries), not the window.
 *
 *   Header      TASK · id · project   [⋯] [×]   + title (max 2 lines)
 *   State card  NEEDS YOU (embedded ApprovalCard) / RUNNING / RESULT /
 *               FAILED — deriveStateCard() decides, none for inbox/review
 *   Facts       STATUS (dropdown) · AGENT · TIME · PR · PLAN · COST
 *   Actions     run control + review (TaskActions), only when relevant
 *   Tabs        Summary · Comments · Deliverables · (Workspace) ·
 *               (Transcript) · Timeline · History
 *
 * Summary (run record JSON) is the default tab, Comments while running. The
 * tab can be controlled from the URL (`tab` / `onTabChange`). The thread is
 * not a tab any more — it is a channel across cards, opened from the ⋯ menu.
 * The E2E tab is gone. A status change the backend refuses (409
 * invalid_transition) is shown as one sentence; Done/Aborted ask first.
 */

import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useLocale, useTranslations } from "next-intl";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AnimatePresence, motion } from "framer-motion";
import {
  ArrowLeft, Check, ChevronDown, ClipboardCopy, Link2, MessagesSquare, MoreHorizontal, Save, Trash2, X,
} from "lucide-react";
import { api } from "@/lib/api";
import { notify } from "@/lib/notify";
import { C, LANE, STATUS_TEXT, alpha } from "@/lib/colors";
import { useAppStore } from "@/lib/store";
import { ConfirmDialog } from "@/components/shared/ConfirmDialog";
import { TaskDescription } from "./TaskDescription";
import { TaskActions } from "./TaskActions";
import { TaskComments } from "./TaskComments";
import { TaskHistory } from "./TaskHistory";
import { TaskTimeline } from "./TaskTimeline";
import { TaskTranscript } from "./TaskTranscript";
import { DeliverablesTab } from "./DeliverablesTab";
import { WorkspaceTab } from "./WorkspaceTab";
import { ThreadPanel } from "./ThreadPanel";
import { GitPanel, gitSectionInfo } from "./GitPanel";
import { TaskReferences } from "./TaskReferences";
import { TaskStateCard } from "./detail/TaskStateCard";
import { TaskFactRow } from "./detail/TaskFactRow";
import { TaskSummaryTab } from "./detail/TaskSummaryTab";
import { deriveStateCard } from "@/lib/taskDetail/stateCard";
import { HeadRunsList } from "@/components/heads/HeadRunsList";
import { useHeadPairsForLabels } from "@/components/heads/HeadStateCard";
import { useHeadsEnabled } from "@/components/heads/useHeadsEnabled";
import { HEAD_POLL_MS, headRunsActive, runPairLabel, sortRunsNewestFirst } from "@/lib/heads";
import { formatAbsolute, formatAge } from "@/lib/taskDetail/format";
import { parseInvalidTransition } from "@/lib/taskDetail/errors";
import { STATUS_LABEL_KEY, statusLabelKey } from "@/lib/taskDetail/statusLabels";
import { defaultTabFor, resolveTab, type TaskTabKey } from "@/lib/taskDetail/tabs";
import type { Agent, Task, TaskChecklistItem, TaskEvent, TaskGitInfo, TaskStatus } from "@/lib/types";

// ── Status vocabulary ────────────────────────────────────────────────────────

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

/** End states ask first — the confirm names the consequence. */
const CONFIRM_STATUS: Partial<Record<TaskStatus, { title: string; body: string; action: string }>> = {
  done: { title: "detail.confirmDoneTitle", body: "detail.confirmDoneBody", action: "detail.confirmDoneAction" },
  aborted: { title: "detail.confirmAbortTitle", body: "detail.confirmAbortBody", action: "detail.confirmAbortAction" },
};

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
        className="inline-flex items-center gap-1.5 rounded-md px-2 py-1 pointer-coarse:min-h-[44px] pointer-coarse:px-3 text-[11px] font-medium cursor-pointer transition-opacity hover:opacity-85"
        style={{ background: alpha(color, 0.12), border: `1px solid ${alpha(color, 0.33)}`, color }}
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
                  className="w-full flex items-center gap-2 px-3 py-1.5 pointer-coarse:min-h-[44px] text-left text-xs transition-colors cursor-pointer disabled:cursor-default"
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

// ── ⋯ menu ───────────────────────────────────────────────────────────────────
//
// Copy link · Copy as Markdown · Save to Vault · Open thread · ──── · Delete.
// Delete sits last, behind a divider, and asks first (two-step).

type MenuItem = {
  key: string;
  label: string;
  icon: typeof Link2;
  onSelect: () => void;
  disabled?: boolean;
  /** Why it is disabled — shown under the label. */
  reason?: string;
};

function OverflowMenu({
  items,
  isActive,
  onDelete,
  deleteLoading,
}: {
  items: MenuItem[];
  isActive: boolean;
  onDelete: () => void;
  deleteLoading: boolean;
}) {
  const t = useTranslations("tasks");
  const [confirm, setConfirm] = useState(false);
  // Portaled with fixed positioning + viewport clamp (see usePortalMenu);
  // right-aligned to the trigger like the old `right-0` dropdown.
  const { open, setOpen, toggle, pos, triggerRef, menuRef } = usePortalMenu({ width: 220, align: "right" });
  // Closing the menu always resets the two-step delete confirm.
  useEffect(() => {
    if (!open) setConfirm(false);
  }, [open]);

  const rowClass = "w-full flex items-start gap-2 px-3 py-2 pointer-coarse:min-h-[44px] pointer-coarse:items-center text-left text-xs transition-colors cursor-pointer disabled:cursor-not-allowed";

  return (
    <div className="relative" ref={triggerRef}>
      <button
        type="button"
        onClick={toggle}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={t("moreActions")}
        className="w-[30px] h-[30px] pointer-coarse:w-11 pointer-coarse:h-11 rounded-md flex items-center justify-center transition-colors hover:bg-[var(--color-bg-hover)] cursor-pointer"
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
            className="w-[220px] rounded-md py-1"
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
            {items.map((item) => {
              const Icon = item.icon;
              return (
                <button
                  key={item.key}
                  role="menuitem"
                  disabled={item.disabled}
                  aria-disabled={item.disabled}
                  onClick={() => {
                    setOpen(false);
                    item.onSelect();
                  }}
                  className={`${rowClass} hover:bg-[var(--color-bg-hover)] disabled:hover:bg-transparent`}
                  style={{ color: item.disabled ? C.textMuted : C.textSecondary }}
                >
                  <Icon size={12} className="mt-0.5 shrink-0" />
                  <span className="min-w-0">
                    <span className="block">{item.label}</span>
                    {item.disabled && item.reason && (
                      <span className="block text-[10px]" style={{ color: C.textMuted }}>
                        {item.reason}
                      </span>
                    )}
                  </span>
                </button>
              );
            })}
            <div role="separator" className="my-1 h-px" style={{ background: C.border }} />
            {!confirm ? (
              <button
                role="menuitem"
                onClick={() => setConfirm(true)}
                className={`${rowClass} hover:bg-[var(--color-bg-hover)]`}
                style={{ color: C.textSecondary }}
              >
                <Trash2 size={12} className="mt-0.5 shrink-0" style={{ color: STATUS_TEXT.error }} />
                {t("deleteTask")}
              </button>
            ) : (
              <div className="px-3 py-2 space-y-2">
                <div className="text-[11px]" style={{ color: C.textSecondary }}>
                  {isActive ? t("deleteWhileActive") : t("deleteConfirm")}
                </div>
                <div className="flex gap-1.5 pointer-coarse:gap-4">
                  <button
                    onClick={onDelete}
                    disabled={deleteLoading}
                    className="px-2 py-1 pointer-coarse:min-h-[44px] pointer-coarse:px-3 pointer-coarse:text-xs rounded-sm text-[10px] font-semibold cursor-pointer"
                    style={{ backgroundColor: alpha(C.error, 0.15), color: STATUS_TEXT.error }}
                  >
                    {deleteLoading ? "…" : t("deleteTask")}
                  </button>
                  <button
                    onClick={() => {
                      setConfirm(false);
                      setOpen(false);
                    }}
                    className="px-2 py-1 pointer-coarse:min-h-[44px] pointer-coarse:px-3 pointer-coarse:text-xs rounded-sm text-[10px] cursor-pointer"
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
              boxShadow: `0 4px 24px ${alpha(C.shadow, 0.5)}, 0 1px 2px ${alpha(C.shadow, 0.3)}`,
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

export function TaskDetailBody({
  task,
  agents,
  boardId,
  onClose,
  tab,
  onTabChange,
  onOpenTask,
  hideCloseOnMobile = false,
}: {
  task: Task;
  agents: Agent[];
  boardId: string;
  onClose: () => void;
  /** Requested tab (e.g. from `?tab=`). Unknown/unavailable → status default. */
  tab?: string | null;
  /** Called on every tab switch — the /tasks page writes it into the URL. */
  onTabChange?: (tab: TaskTabKey) => void;
  /** Open another task in place (subtask links); without it links navigate. */
  onOpenTask?: (taskId: string) => void;
  /** The /tasks page has its own "‹ Tasks" bar on the phone — no second ×. */
  hideCloseOnMobile?: boolean;
}) {
  const t = useTranslations("tasks");
  const locale = useLocale();
  const qc = useQueryClient();
  const bodyRef = useRef<HTMLDivElement>(null);

  // Uncontrolled fallback: default tab per status, reset when another task
  // opens in the same body (render-time reset, no flash of the old tab).
  const [tabState, setTabState] = useState<{ taskId: string; tab: TaskTabKey }>(() => ({
    taskId: task.id,
    tab: defaultTabFor(task.status),
  }));
  if (tabState.taskId !== task.id) {
    setTabState({ taskId: task.id, tab: defaultTabFor(task.status) });
  }

  const agent = agents.find((a) => a.id === task.assigned_agent_id);
  const isActive = task.status === "in_progress" || task.status === "review";
  const currentUser = useAppStore((s) => s.currentUser);
  const [confirmStatus, setConfirmStatus] = useState<TaskStatus | null>(null);
  // Bumped by "Reply" — TaskComments focuses its input on every change, also
  // when Comments is already the open tab.
  const [focusCommentSignal, setFocusCommentSignal] = useState(0);

  const statusWord = (s: string) => {
    const key = statusLabelKey(s);
    return key ? t(key) : s;
  };

  // ── Tabs ───────────────────────────────────────────────────────────────────

  const tabs: { key: TaskTabKey; label: string }[] = [
    { key: "summary", label: t("detail.tabSummary") },
    { key: "comments", label: t("tabComments") },
    { key: "deliverables", label: t("tabDeliverables") },
    ...(task.workspace_path ? [{ key: "workspace" as const, label: t("tabWorkspace") }] : []),
    ...(task.spawn_session_key || task.dispatched_at ? [{ key: "transcript" as const, label: t("tabTranscript") }] : []),
    { key: "timeline", label: t("tabTimeline") },
    { key: "history", label: t("tabHistory") },
  ];
  // "thread" is reachable (⋯ menu, ?tab=thread) but not in the strip.
  const available: TaskTabKey[] = [...tabs.map((x) => x.key), "thread"];
  const internalTab = tabState.taskId === task.id ? tabState.tab : defaultTabFor(task.status);
  const activeTab = resolveTab(tab ?? internalTab, available, task.status);

  function selectTab(next: TaskTabKey) {
    setTabState({ taskId: task.id, tab: next });
    onTabChange?.(next);
  }

  // Roving focus in the tab strip: ←/→ and Home/End move and select.
  function onTabKeyDown(e: React.KeyboardEvent<HTMLButtonElement>) {
    const keys = tabs.map((x) => x.key);
    const i = keys.indexOf(activeTab);
    let next: number | null = null;
    if (e.key === "ArrowRight") next = (i + 1) % keys.length;
    else if (e.key === "ArrowLeft") next = (i - 1 + keys.length) % keys.length;
    else if (e.key === "Home") next = 0;
    else if (e.key === "End") next = keys.length - 1;
    if (next == null) return;
    e.preventDefault();
    selectTab(keys[next]);
    const strip = e.currentTarget.parentElement;
    window.setTimeout(() => strip?.querySelector<HTMLButtonElement>(`[data-tab-key="${keys[next!]}"]`)?.focus(), 0);
  }
  const tabId = (key: string) => `task-${task.id}-tab-${key}`;
  const panelId = `task-${task.id}-panel`;

  // ── Mutations ──────────────────────────────────────────────────────────────

  const updateMutation = useMutation({
    mutationFn: (data: Partial<Task>) => api.tasks.update(boardId, task.id, data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["tasks", boardId] });
      qc.invalidateQueries({ queryKey: ["pipeline", boardId] });
      qc.invalidateQueries({ queryKey: ["task", boardId, task.id] });
      qc.invalidateQueries({ queryKey: ["run-record", task.id] });
    },
    onError: (e: Error) => {
      // 409 invalid_transition → one sentence. The allowed transitions stay
      // the backend's business; we do not model them here.
      const refused = parseInvalidTransition(e);
      if (refused) {
        notify.error(t("detail.transitionNotAllowed", { from: statusWord(refused.current) }));
        return;
      }
      notify.error(t("updateFailed", { msg: e.message }));
    },
  });

  function requestStatus(s: TaskStatus) {
    if (CONFIRM_STATUS[s]) {
      setConfirmStatus(s);
      return;
    }
    updateMutation.mutate({ status: s } as Partial<Task>);
  }

  const deleteMutation = useMutation({
    mutationFn: () => api.tasks.delete(boardId, task.id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["tasks", boardId] });
      qc.invalidateQueries({ queryKey: ["pipeline", boardId] });
      onClose();
    },
    onError: (e: Error) => notify.error(t("deleteFailed", { msg: e.message })),
  });

  const vaultMutation = useMutation({
    mutationFn: () => api.tasks.runRecordToVault(task.id),
    onSuccess: (res) => notify.success(t("detail.savedToVault", { path: res.path })),
    onError: (e: Error) => notify.error(t("detail.saveFailed", { msg: e.message })),
  });

  // ── Queries ────────────────────────────────────────────────────────────────

  const runRecordQuery = useQuery({
    queryKey: ["run-record", task.id],
    queryFn: () => api.tasks.runRecord(task.id),
    staleTime: 15_000,
    refetchInterval: 60_000,
  });
  const runRecord = runRecordQuery.data;

  // Head runs of this task (head launcher §8.2). Polls every 10 s only while
  // the newest run is active, otherwise not at all. Heads off → not asked.
  const headsEnabled = useHeadsEnabled();
  const headRunsQuery = useQuery({
    queryKey: ["heads", "task", task.id],
    queryFn: () => api.heads.list({ taskId: task.id }),
    enabled: headsEnabled === true,
    retry: false,
    refetchInterval: (query) => (headRunsActive(query.state.data) ? HEAD_POLL_MS : false),
  });
  const headRuns = Array.isArray(headRunsQuery.data?.runs) ? headRunsQuery.data.runs : [];
  const latestHeadRun = sortRunsNewestFirst(headRuns)[0] ?? null;
  const headPairs = useHeadPairsForLabels(headRuns.length > 0);

  const needsApprovals = task.status === "blocked" || task.status === "waiting" || task.status === "user_test";
  // Same query key as the inbox — one cache, one source of truth.
  const { data: approvals = [] } = useQuery({
    queryKey: ["approvals"],
    queryFn: api.approvals.list,
    enabled: needsApprovals,
    refetchInterval: 15_000,
  });

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

  // Shared query key with TaskComments — feeds the state card (last step,
  // blocker reason, resolution) and is a cache hit on the Comments tab.
  const { data: comments = [] } = useQuery({
    queryKey: ["task-comments", task.id],
    queryFn: () => api.tasks.comments.list(boardId, task.id),
  });

  const { data: gitInfo } = useQuery<TaskGitInfo>({
    queryKey: ["task-git-info", boardId, task.id],
    queryFn: () => api.tasks.gitInfo(boardId, task.id),
    enabled: !!task.workspace_path,
    refetchInterval: 30_000,
  });
  const gitSection = gitSectionInfo(gitInfo, task.pr_url);

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

  // ── Derived ────────────────────────────────────────────────────────────────

  const stateCard = deriveStateCard({ task, approvals, comments, runRecord: runRecord ?? null, headRun: latestHeadRun });

  const briefingFields: { label: string; value: string | null | undefined }[] = task.intake_mode
    ? [
        { label: t("briefType"), value: task.request_kind },
        { label: t("briefOutput"), value: task.desired_output },
        { label: t("briefOutOfScope"), value: task.scope_out },
        { label: t("briefRisks"), value: task.risk_notes },
        { label: t("briefCriteria"), value: task.acceptance_criteria },
        { label: t("briefBrowser"), value: task.needs_browser ? t("yes") : null },
        { label: t("briefCredentials"), value: task.requires_auth ? t("yes") : null },
        { label: t("briefApproval"), value: task.approval_policy },
        { label: t("briefAutonomy"), value: task.autonomy_level },
        { label: t("briefLinks"), value: task.reference_urls?.join(", ") || null },
        { label: t("briefNotes"), value: task.reference_notes },
      ].filter((f) => f.value)
    : [];

  const checklistDone = checklist.filter((i) => i.status === "done").length;
  const projectName = task.project_id ? (projects.find((p) => p.id === task.project_id)?.name ?? t("projectFallback")) : t("adHoc");

  // A head owns this task's run (head launcher §8.2): the fleet run controls
  // (Run held / Requeue / Stop agent) would hand it back to the frozen
  // dispatch — hidden; the head card carries Stop / Restart / Open PR
  // instead. Also in review after a passed head: the card keeps its hold
  // (spec §6.6), so TaskActions would only show "Review blocked" next to
  // Requeue — the merge decision happens on the PR, the status menu moves
  // the card to done.
  const headOwnsRun = latestHeadRun != null;

  // TaskActions only renders something in these cases — no empty section.
  const showActions =
    !headOwnsRun && (
    (task.dispatch_phase === "planning" && !!task.parent_task_id) ||
    task.status === "in_progress" ||
    task.status === "review" ||
    (task.status === "inbox" && task.dispatched_at != null) ||
    task.run_control === "stopped" ||
    task.run_control === "manual_hold");

  // Save to Vault writes — operator role (backend: require_role(OPERATOR)).
  const canSaveToVault = currentUser?.role === "operator" || currentUser?.role === "admin";

  function copyFailed(e: unknown) {
    notify.error(t("detail.copyFailed", { msg: e instanceof Error ? e.message : String(e) }));
  }

  async function copyText(text: string, okMessage: string) {
    try {
      await navigator.clipboard.writeText(text);
      notify.success(okMessage);
    } catch (e) {
      copyFailed(e);
    }
  }

  /**
   * Copy text that still has to be fetched. Safari only allows a clipboard
   * write right inside the click — not after an await — so the write starts
   * synchronously with a ClipboardItem that holds the pending text. Browsers
   * without ClipboardItem fall back to writeText after the fetch.
   */
  function copyFetched(fetchText: () => Promise<string>, okMessage: string) {
    const text = fetchText();
    const canWriteItem = typeof ClipboardItem !== "undefined" && typeof navigator.clipboard?.write === "function";
    if (!canWriteItem) {
      void text.then((s) => copyText(s, okMessage), copyFailed);
      return;
    }
    let written: Promise<void>;
    try {
      written = navigator.clipboard.write([
        new ClipboardItem({ "text/plain": text.then((s) => new Blob([s], { type: "text/plain" })) }),
      ]);
    } catch (e) {
      written = Promise.reject(e);
    }
    written.then(
      () => notify.success(okMessage),
      // A browser that knows ClipboardItem but refuses a pending item (older
      // Chromium/Firefox) still gets the text via writeText.
      () => text.then((s) => copyText(s, okMessage), copyFailed),
    );
  }

  const menuItems: MenuItem[] = [
    {
      key: "copy-link",
      label: t("detail.copyLink"),
      icon: Link2,
      onSelect: () =>
        copyText(`${window.location.origin}/tasks?task=${task.id}&tab=${activeTab}`, t("detail.linkCopied")),
    },
    {
      key: "copy-markdown",
      label: t("detail.copyMarkdown"),
      icon: ClipboardCopy,
      onSelect: () => copyFetched(() => api.tasks.runRecordMarkdown(task.id), t("detail.markdownCopied")),
    },
    {
      key: "save-vault",
      label: t("detail.saveToVault"),
      icon: Save,
      disabled: !canSaveToVault || vaultMutation.isPending,
      reason: !canSaveToVault ? t("detail.saveToVaultNeedsRole") : undefined,
      onSelect: () => vaultMutation.mutate(),
    },
    {
      key: "thread",
      label: agent ? t("detail.openAgentThread", { name: agent.name }) : t("detail.openThread"),
      icon: MessagesSquare,
      onSelect: () => selectTab("thread"),
    },
  ];

  const confirm = confirmStatus ? CONFIRM_STATUS[confirmStatus] : undefined;

  return (
    <>
      {/* ── Header ── */}
      <div className="px-4 pt-4 pb-3 shrink-0" style={{ borderBottom: `1px solid ${C.border}` }}>
        <div className="flex items-start gap-3">
          <div className="flex-1 min-w-0">
            <div className="label-sys label-sys--dim mb-1.5 truncate">
              {t("taskLabel")} · {task.id.slice(0, 8)} · {projectName}
            </div>
            <h2
              className="text-[18px] font-semibold leading-snug line-clamp-2"
              style={{ color: C.textPrimary }}
              title={task.title}
            >
              {task.title}
            </h2>
          </div>
          <div className="flex items-center gap-1.5 shrink-0">
            <OverflowMenu
              items={menuItems}
              isActive={isActive}
              onDelete={() => deleteMutation.mutate()}
              deleteLoading={deleteMutation.isPending}
            />
            <button
              onClick={onClose}
              aria-label={t("closeTaskDetails")}
              className={`${hideCloseOnMobile ? "hidden md:flex" : "flex"} w-[30px] h-[30px] pointer-coarse:w-11 pointer-coarse:h-11 rounded-md items-center justify-center transition-colors hover:bg-[var(--color-bg-hover)] cursor-pointer`}
              style={{ color: C.textSecondary, border: `1px solid ${C.border}` }}
            >
              <X size={15} />
            </button>
          </div>
        </div>
      </div>

      {/* ── Scrollable body — the @container the facts row and tabs measure ── */}
      <div
        ref={bodyRef}
        className="@container flex-1 overflow-y-auto"
        style={{ overscrollBehavior: "contain", WebkitOverflowScrolling: "touch" } as React.CSSProperties}
      >
        <div className="px-4 pt-3 pb-3 space-y-3">
          {stateCard && (
            <TaskStateCard
              card={stateCard}
              task={task}
              agents={agents}
              onReply={() => {
                selectTab("comments");
                setFocusCommentSignal((n) => n + 1);
              }}
              onOpenLog={() => selectTab("timeline")}
            />
          )}
          <TaskFactRow
            task={task}
            agent={agent}
            runRecord={runRecord}
            headFact={latestHeadRun ? runPairLabel(latestHeadRun, headPairs) : null}
            checklist={{ done: checklistDone, total: checklist.length }}
            statusControl={
              <StatusMenu status={task.status} pending={updateMutation.isPending} onChange={requestStatus} />
            }
          />
        </div>

        {showActions && (
          <Section>
            <TaskActions task={task} boardId={boardId} />
          </Section>
        )}

        {/* Tabs — strip from 560 px container width, a select below it */}
        <div className="px-4 pt-1 @min-[560px]:hidden">
          <label className="sr-only" htmlFor={`task-tab-select-${task.id}`}>{t("detail.sectionSelect")}</label>
          <select
            id={`task-tab-select-${task.id}`}
            value={activeTab}
            onChange={(e) => selectTab(e.target.value as TaskTabKey)}
            className="w-full min-h-[44px] px-3 rounded-md text-base font-mono uppercase tracking-[0.08em]"
            style={{ background: C.bgDeep, color: C.textPrimary, border: `1px solid ${C.border}` }}
          >
            {tabs.map((x) => (
              <option key={x.key} value={x.key}>{x.label}</option>
            ))}
            {activeTab === "thread" && <option value="thread">{t("detail.thread")}</option>}
          </select>
        </div>
        <div
          className="hidden @min-[560px]:flex gap-0.5 px-4 tab-strip"
          style={{ borderBottom: `1px solid ${C.border}` }}
          role="tablist"
        >
          {tabs.map((x) => {
            const active = activeTab === x.key;
            return (
              <button
                key={x.key}
                id={tabId(x.key)}
                data-tab-key={x.key}
                role="tab"
                aria-selected={active}
                aria-controls={panelId}
                tabIndex={active ? 0 : -1}
                onKeyDown={onTabKeyDown}
                onClick={() => selectTab(x.key)}
                className="px-2.5 py-2 font-mono text-[10px] uppercase tracking-[0.12em] cursor-pointer transition-colors -mb-px"
                style={{
                  color: active ? C.accent : C.textMuted,
                  fontWeight: active ? 500 : 400,
                  borderBottom: `2px solid ${active ? C.accent : "transparent"}`,
                }}
              >
                {x.label}
              </button>
            );
          })}
        </div>

        <div
          className="px-4 py-3 pb-4"
          role="tabpanel"
          id={panelId}
          aria-labelledby={tabs.some((x) => x.key === activeTab) ? tabId(activeTab) : undefined}
          data-tab={activeTab}
        >
          {activeTab === "summary" ? (
            <TaskSummaryTab
              task={task}
              runRecord={runRecord}
              isLoading={runRecordQuery.isLoading}
              isError={runRecordQuery.isError}
              onRetry={() => runRecordQuery.refetch()}
              subtasks={hierarchy?.children ?? []}
              checklist={checklist}
              onOpenTask={onOpenTask}
              leading={headRuns.length > 0 ? <HeadRunsList runs={headRuns} pairs={headPairs} /> : undefined}
              briefExtra={
                briefingFields.length > 0 ? (
                  <div className="mt-2 space-y-1">
                    <div className="label-sys label-sys--dim">{t("briefing")} · {task.intake_mode}</div>
                    {briefingFields.map((f) => (
                      <div key={f.label} className="text-xs">
                        <span style={{ color: C.textMuted }}>{f.label}: </span>
                        <span style={{ color: C.textPrimary }}>{f.value}</span>
                      </div>
                    ))}
                  </div>
                ) : undefined
              }
            >
              {/* Properties */}
              <Section>
                <SectionLabel>{t("properties")}</SectionLabel>
                <div
                  className="grid grid-cols-2 gap-px rounded-lg overflow-hidden"
                  style={{ background: C.border, border: `1px solid ${C.border}` }}
                >
                  <PropertyMenuCell
                    label={t("assignee")}
                    value={agent ? agent.name : t("unassigned")}
                    options={agents.map((a) => ({ id: a.id, label: a.name, active: a.id === task.assigned_agent_id }))}
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
                      {creatorName ?? "—"} · <span title={formatAbsolute(task.created_at, locale)}>{t("detail.ago", { age: formatAge(task.created_at, locale) ?? "—" })}</span>
                    </span>
                  </div>
                  <div className="px-2.5 py-2" style={{ background: C.bgSurface }}>
                    <span className="block text-[9px] font-semibold uppercase tracking-[0.07em] mb-0.5" style={{ color: C.textDim }}>
                      {t("started")}
                    </span>
                    <span className="text-xs" style={{ color: C.textPrimary }}>
                      {task.started_at ? (
                        <span title={formatAbsolute(task.started_at, locale)}>{t("detail.ago", { age: formatAge(task.started_at, locale) ?? "—" })}</span>
                      ) : "—"}
                    </span>
                  </div>
                </div>
              </Section>

              {/* Relations — subtasks live in STEPS above */}
              {(hierarchy?.parent || (dependencies?.length ?? 0) > 0) && (
                <Section>
                  <SectionLabel>{t("relations")}</SectionLabel>
                  <div className="space-y-2">
                    {hierarchy?.parent && (
                      <div className="flex items-center gap-2 text-xs">
                        <span className="shrink-0" style={{ color: C.textMuted }}>
                          {t("parent")}
                        </span>
                        <span className="w-1.5 h-1.5 rounded-full shrink-0" style={{ background: LANE[hierarchy.parent.status] ?? C.textMuted }} />
                        <a
                          href={`/tasks?task=${hierarchy.parent.id}`}
                          onClick={(e) => {
                            if (!onOpenTask || e.metaKey || e.ctrlKey) return;
                            e.preventDefault();
                            onOpenTask(hierarchy.parent!.id);
                          }}
                          className="truncate hover:underline"
                          style={{ color: C.textSecondary }}
                          title={hierarchy.parent.title}
                        >
                          {hierarchy.parent.title}
                        </a>
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
                              <span style={{ color: C.textMuted }}>({statusWord(dep.status)})</span>
                            </div>
                          ))}
                        </div>
                      </div>
                    )}
                  </div>
                </Section>
              )}

              {/* References (ADR-053) */}
              <Section>
                <SectionLabel>{t("references")}</SectionLabel>
                <TaskReferences taskId={task.id} />
              </Section>

              {/* Git */}
              {gitSection && (
                <Section last>
                  <SectionLabel>Git</SectionLabel>
                  <GitPanel
                    gitInfo={gitSection}
                    boardId={boardId}
                    taskId={task.id}
                    taskPrUrl={task.pr_url}
                    taskPrNumber={task.pr_number}
                  />
                </Section>
              )}
            </TaskSummaryTab>
          ) : activeTab === "thread" ? (
            <div>
              <button
                type="button"
                onClick={() => selectTab("summary")}
                className="inline-flex items-center gap-1.5 mb-3 text-[11px] pointer-coarse:min-h-[44px] cursor-pointer hover:underline"
                style={{ color: C.textSecondary }}
              >
                <ArrowLeft size={12} aria-hidden />
                {t("detail.backToSummary")}
              </button>
              <ThreadPanel taskId={task.id} />
            </div>
          ) : activeTab === "comments" ? (
            <TaskComments task={task} boardId={boardId} agents={agents} focusSignal={focusCommentSignal} />
          ) : activeTab === "transcript" ? (
            <TaskTranscript taskId={task.id} isLive={task.status === "in_progress" || task.status === "review"} />
          ) : activeTab === "deliverables" ? (
            <DeliverablesTab deliverables={deliverables ?? []} boardId={boardId} taskId={task.id} />
          ) : activeTab === "workspace" ? (
            <WorkspaceTab task={task} boardId={boardId} />
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

      <ConfirmDialog
        open={!!confirm}
        kicker={t("detail.confirmKicker")}
        title={confirm ? t(confirm.title) : ""}
        body={confirm ? t(confirm.body) : undefined}
        confirmLabel={confirm ? t(confirm.action) : undefined}
        cancelLabel={t("cancel")}
        danger={confirmStatus === "aborted"}
        onCancel={() => setConfirmStatus(null)}
        onConfirm={() => {
          const s = confirmStatus;
          setConfirmStatus(null);
          if (s) updateMutation.mutate({ status: s } as Partial<Task>);
        }}
      />
    </>
  );
}
