"use client";

/**
 * TaskDetailBody — shared content of the task detail (wave 3a "task detail
 * lite", 09/2026; header rebuilt as variant A, DESIGN.md K12). One body for
 * three chromes: the /tasks split (~800 px), the Home modal (~672 px) and the
 * phone (390 px) — the layout follows the width of the CONTAINER (CSS
 * container queries), not the window.
 *
 *   Context bar  ‹ Tasks (phone)  [● title once the title scrolled away]  ⋯  ×
 *   Title        max 3 lines
 *   State line   ● Blocked · Rex asked 42 min ago — deriveStateLine()
 *   Next step    only when there is one: NEEDS YOU (embedded ApprovalCard or
 *                blocker + Reply) / RUNNING (last step) / RESULT (resolution +
 *                PR) / FAILED (error + Open log) / head — deriveStateCard()
 *   Actions      run control + review (TaskActions), only when relevant
 *   Tabs         Summary · Comments · Deliverables · (Workspace) ·
 *                (Transcript) · Timeline · History — sticky, sentence case
 *
 * Properties (status menu, assignee, project, priority, cost, run tonight,
 * id …) are a calm list in the Summary tab, a right column from 720 px
 * container width. Summary (run record JSON) is the default tab, Comments
 * while running. The tab can be controlled from the URL (`tab` /
 * `onTabChange`). The thread is not a tab — it is a channel across cards,
 * opened from the ⋯ menu. A status change the backend refuses (409
 * invalid_transition) is shown as one sentence; Done/Aborted ask first.
 *
 * The chrome sets `--detail-bg` (its own background, for the sticky tabs) and
 * `--detail-raised` (the "you have to act" surface one step above it).
 */

import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useLocale, useTranslations } from "next-intl";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AnimatePresence, motion } from "framer-motion";
import {
  ArrowLeft, Check, ChevronDown, ChevronLeft, CircleDot, ClipboardCopy, Copy, ExternalLink, Link2, MessagesSquare, MoreHorizontal, Save, Trash2, X,
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
import { TaskStateCard, TaskStateLine, stateDotColor } from "./detail/TaskStateCard";
import { PropRow, TaskProperties } from "./detail/TaskProperties";
import { TaskSummaryTab } from "./detail/TaskSummaryTab";
import { deriveStateCard } from "@/lib/taskDetail/stateCard";
import { deriveStateLine } from "@/lib/taskDetail/stateLine";
import { HeadRunsList } from "@/components/heads/HeadRunsList";
import { useHeadPairsForLabels } from "@/components/heads/HeadStateCard";
import { useHeadsEnabled } from "@/components/heads/useHeadsEnabled";
import { NightShiftToggle } from "@/components/night/NightShiftToggle";
import { canMarkTonight } from "@/lib/nightShift";
import { HEAD_POLL_MS, headRunsActive, isHeadActive, runPairLabel, sortRunsNewestFirst } from "@/lib/heads";
import { formatAbsolute, formatAge, formatDuration, formatUsd, secondsBetween } from "@/lib/taskDetail/format";
import { parseInvalidTransition } from "@/lib/taskDetail/errors";
import { STATUS_LABEL_KEY, statusLabelKey } from "@/lib/taskDetail/statusLabels";
import { defaultTabFor, resolveTab, type TaskTabKey } from "@/lib/taskDetail/tabs";
import type { Agent, Task, TaskChecklistItem, TaskEvent, TaskGitInfo, TaskStatus } from "@/lib/types";

/** A night run makes no sense on a closed card. */
const FINISHED_STATUSES: ReadonlySet<string> = new Set(["done", "aborted"]);

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
  openRequest = 0,
  onOpenHandled,
}: {
  status: TaskStatus;
  onChange: (s: TaskStatus) => void;
  pending: boolean;
  /** Bumped by ⋯ "Change status": scroll the row into view and open the menu. */
  openRequest?: number;
  onOpenHandled?: () => void;
}) {
  const t = useTranslations("tasks");
  const color = LANE[status] ?? C.textMuted;
  // Portaled with fixed positioning + viewport clamp (see usePortalMenu) so
  // the menu can never run off the right edge on narrow (393px) viewports.
  const { open, setOpen, toggle, pos, triggerRef, menuRef } = usePortalMenu({ width: 150 });

  useEffect(() => {
    if (!openRequest) return;
    triggerRef.current?.scrollIntoView?.({ block: "center" });
    // Measure after the scroll has landed; the menu is fixed-positioned.
    const id = window.requestAnimationFrame(() => {
      if (!open) toggle();
      triggerRef.current?.querySelector("button")?.focus();
      onOpenHandled?.();
    });
    return () => window.cancelAnimationFrame(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- only on a new request
  }, [openRequest]);

  return (
    <div className="relative" ref={triggerRef}>
      <button
        type="button"
        onClick={toggle}
        disabled={pending}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={t("statusChange", { label: t(STATUS_LABEL_KEY[status]) })}
        className="inline-flex items-center gap-2 h-11 -ml-2 px-2 rounded-md text-sm cursor-pointer transition-colors hover:bg-[var(--color-bg-hover)]"
        style={{ color: C.textPrimary }}
      >
        <span aria-hidden className="w-2 h-2 rounded-full" style={{ background: color }} />
        {t(STATUS_LABEL_KEY[status])}
        <ChevronDown size={16} aria-hidden style={{ color: C.textMuted }} />
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
// Change status · Copy link · Copy as Markdown · Save to Vault · Open thread ·
// ──── · Delete.
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
        className="w-11 h-11 rounded-md flex items-center justify-center transition-colors hover:bg-[var(--color-bg-hover)] cursor-pointer"
        style={{ color: C.textSecondary }}
      >
        <MoreHorizontal size={20} />
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

// ── Property dropdowns (assignee / project) ──────────────────────────────────

function PropertyMenu({
  label,
  value,
  options,
  onSelect,
}: {
  /** The row's label — names the control for screen readers. */
  label: string;
  value: string;
  options: { id: string | null; label: string; active: boolean }[];
  onSelect: (id: string | null) => void;
}) {
  // Rendered through a portal with fixed positioning measured off the
  // trigger: the list sits inside a scroll container that would cut an
  // absolute dropdown off; usePortalMenu also clamps it into the viewport.
  const MENU_MAX = 240;
  const { open, setOpen, toggle, pos, triggerRef, menuRef } = usePortalMenu({ width: 220, flipMax: MENU_MAX });

  return (
    <div className="relative flex-1 min-w-0" ref={triggerRef}>
      <button
        type="button"
        onClick={toggle}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={`${label}: ${value}`}
        className="inline-flex max-w-full items-center gap-2 h-11 -ml-2 px-2 rounded-md text-sm text-left cursor-pointer transition-colors hover:bg-[var(--color-bg-hover)]"
        style={{ color: C.textPrimary }}
      >
        <span className="truncate">{value}</span>
        <ChevronDown size={16} aria-hidden className="shrink-0" style={{ color: C.textMuted }} />
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
              width: 220,
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
                className="w-full flex items-center gap-2 px-3 py-2 pointer-coarse:min-h-[44px] text-left text-sm transition-colors cursor-pointer"
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
  onBack,
  backLabel,
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
  /** Phone: the /tasks page shows "‹ Tasks" in the context bar instead of ×. */
  hideCloseOnMobile?: boolean;
  /** Phone back link in the context bar ("‹ Tasks"), shown below md. */
  onBack?: () => void;
  backLabel?: string;
}) {
  const t = useTranslations("tasks");
  const tHeads = useTranslations("heads");
  const locale = useLocale();
  const qc = useQueryClient();
  const bodyRef = useRef<HTMLDivElement>(null);
  const titleRef = useRef<HTMLHeadingElement>(null);

  // The context bar shows "● title" once the real title has scrolled out of
  // view (DESIGN.md K12: the bar replaces, it never adds a third bar).
  const [scrolled, setScrolled] = useState(false);
  useEffect(() => {
    const el = titleRef.current;
    const root = bodyRef.current;
    if (!el || !root || typeof IntersectionObserver === "undefined") return;
    const io = new IntersectionObserver(([e]) => setScrolled(!e.isIntersecting), { root, threshold: 0 });
    io.observe(el);
    return () => io.disconnect();
  }, [task.id]);

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
  // Bumped by ⋯ "Change status" — the status row lives in the Summary tab.
  const [statusMenuRequest, setStatusMenuRequest] = useState(0);

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
  const stateLine = deriveStateLine({ task, card: stateCard, agents, locale });

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
      key: "status",
      label: t("detail.changeStatus"),
      icon: CircleDot,
      onSelect: () => {
        selectTab("summary");
        setStatusMenuRequest((n) => n + 1);
      },
    },
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

  // ── Properties (Summary tab) ───────────────────────────────────────────────
  // Every fact once (K3): what the state line or the next step already says
  // (agent + time, PR, duration of a result) is not repeated here, and empty
  // values ("—", "not tracked") are left out.

  // The PR shows in the header when the header is about it: a result, a card
  // in review, or a head that opened one. Otherwise it is a property.
  const reviewPr = !headOwnsRun && task.status === "review" && !!task.pr_url;
  const prInHeader =
    reviewPr ||
    (stateCard?.kind === "result" && !!stateCard.prUrl) ||
    (stateCard?.kind === "head" && stateCard.mainAction === "open_pr");
  const kosten = runRecord?.kosten;
  const billed = kosten && kosten.gesamt_usd > 0 ? formatUsd(kosten.gesamt_usd) : null;
  const showPriority = task.priority === "high" || task.priority === "critical";
  const failedDuration =
    stateCard?.kind === "failed"
      ? formatDuration(runRecord?.zeiten.dauer_sekunden ?? secondsBetween(task.created_at, task.completed_at), locale)
      : null;
  const heartbeat = stateCard?.kind === "running" ? formatAge(stateCard.heartbeatAt, locale) : null;
  const showNight =
    headsEnabled === true && !!task.repo_id && !FINISHED_STATUSES.has(task.status) && !isHeadActive(latestHeadRun);

  const properties = (
    <TaskProperties title={t("properties")}>
      <PropRow label={t("detail.factStatus")} testId="fact-status">
        <StatusMenu
          status={task.status}
          pending={updateMutation.isPending}
          onChange={requestStatus}
          openRequest={statusMenuRequest}
          onOpenHandled={() => setStatusMenuRequest(0)}
        />
      </PropRow>
      <PropRow label={t("detail.factAgent")} testId="fact-agent">
        <PropertyMenu
          label={t("detail.factAgent")}
          value={agent ? agent.name : t("unassigned")}
          options={agents.map((a) => ({ id: a.id, label: a.name, active: a.id === task.assigned_agent_id }))}
          onSelect={(id) => id && updateMutation.mutate({ assigned_agent_id: id } as Partial<Task>)}
        />
      </PropRow>
      <PropRow label={t("projectFallback")} testId="fact-project">
        <PropertyMenu
          label={t("projectFallback")}
          value={projectName}
          options={[
            { id: null, label: t("adHocNoProject"), active: !task.project_id },
            ...projects.map((p) => ({ id: p.id, label: p.name, active: p.id === task.project_id })),
          ]}
          onSelect={(id) => updateMutation.mutate({ project_id: id } as Partial<Task>)}
        />
      </PropRow>
      {showPriority && (
        <PropRow label={t("detail.factPriority")} testId="fact-priority">
          {task.priority === "critical" ? t("priorityCritical") : t("priorityHigh")}
        </PropRow>
      )}
      {task.pr_url && !prInHeader && (
        <PropRow label={t("detail.factPr")} testId="fact-pr">
          <a
            href={task.pr_url}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1 underline underline-offset-4"
            style={{ textDecorationColor: C.borderActive }}
          >
            {task.pr_number ? t("prChipNumber", { number: task.pr_number }) : t("prChipOpen")}
            <ExternalLink size={14} aria-hidden />
          </a>
        </PropRow>
      )}
      {latestHeadRun && (
        <PropRow label={tHeads("runs.fact")} testId="fact-head">
          <span className="truncate">{runPairLabel(latestHeadRun, headPairs)}</span>
        </PropRow>
      )}
      {stateCard?.kind === "running" && (
        <PropRow label={t("detail.factHeartbeat")} testId="fact-heartbeat">
          {heartbeat ? t("detail.ago", { age: heartbeat }) : t("detail.noHeartbeat")}
        </PropRow>
      )}
      {failedDuration && (
        <PropRow label={t("detail.factDuration")} testId="fact-time">
          <span className="tabular-nums">{failedDuration}</span>
        </PropRow>
      )}
      {billed && (
        <PropRow label={t("detail.factCost")} testId="fact-cost">
          <span className="tabular-nums">{billed}</span>
        </PropRow>
      )}
      {creatorName && (
        <PropRow label={t("createdBy")} testId="fact-creator">
          <span className="truncate">{creatorName}</span>
        </PropRow>
      )}
      {task.started_at && (
        <PropRow label={t("started")} testId="fact-started">
          <span title={formatAbsolute(task.started_at, locale)}>{t("detail.ago", { age: formatAge(task.started_at, locale) ?? "—" })}</span>
        </PropRow>
      )}
      {/* Night shift: "Run tonight" — only for cards a head can take (repo,
          not finished), while no head is working on it right now, and only
          markable while nobody else works on the card. */}
      {showNight && <NightShiftToggle taskId={task.id} canMark={canMarkTonight(task)} variant="row" />}
      <PropRow label={t("detail.factId")} testId="fact-id">
        <span className="font-mono text-sm tabular-nums" title={task.id}>{task.id.slice(0, 8)}</span>
        <button
          type="button"
          aria-label={t("detail.copyId")}
          onClick={() => copyText(task.id, t("detail.idCopied"))}
          className="ml-auto w-11 h-11 rounded-md flex items-center justify-center cursor-pointer transition-colors hover:bg-[var(--color-bg-hover)]"
          style={{ color: C.textSecondary }}
        >
          <Copy size={16} aria-hidden />
        </button>
      </PropRow>
    </TaskProperties>
  );

  const commentCount = comments.length;

  return (
    <>
      {/* ── Context bar — stays put while the body scrolls ── */}
      <div
        data-region="task-context"
        data-scrolled={scrolled || undefined}
        className="shrink-0 h-14 flex items-center gap-1 px-2 transition-colors motion-reduce:transition-none"
        style={{ borderBottom: `1px solid ${scrolled ? C.border : "transparent"}` }}
      >
        {onBack && (
          <button
            type="button"
            onClick={onBack}
            // Named also while scrolled, when the label text is hidden.
            aria-label={backLabel ?? t("detail.backToList")}
            className="md:hidden shrink-0 flex items-center gap-1 h-11 pl-1 pr-3 rounded-md text-base cursor-pointer transition-colors hover:bg-[var(--color-bg-hover)]"
            style={{ color: C.textSecondary }}
          >
            <ChevronLeft size={22} aria-hidden />
            {!scrolled && <span>{backLabel}</span>}
          </button>
        )}
        <div
          aria-hidden={!scrolled}
          data-testid="task-compact-title"
          className="flex-1 min-w-0 flex items-center gap-2 px-2 transition-[opacity,transform] duration-200 motion-reduce:transition-none"
          style={{ opacity: scrolled ? 1 : 0, transform: scrolled ? "none" : "translateY(4px)", pointerEvents: "none" }}
        >
          {/* Mounted only while shown: the real title is the one copy otherwise. */}
          {scrolled && (
            <>
              <span className="w-2 h-2 rounded-full shrink-0" style={{ background: stateDotColor(stateLine.tone) }} />
              <span className="truncate text-sm" style={{ color: C.textPrimary, fontWeight: 500 }}>{task.title}</span>
            </>
          )}
        </div>
        <OverflowMenu
          items={menuItems}
          isActive={isActive}
          onDelete={() => deleteMutation.mutate()}
          deleteLoading={deleteMutation.isPending}
        />
        <button
          onClick={onClose}
          aria-label={t("closeTaskDetails")}
          className={`${hideCloseOnMobile ? "hidden md:flex" : "flex"} w-11 h-11 rounded-md items-center justify-center transition-colors hover:bg-[var(--color-bg-hover)] cursor-pointer`}
          style={{ color: C.textSecondary }}
        >
          <X size={20} />
        </button>
      </div>

      {/* ── Scrollable body — the @container the header, tabs and summary measure ── */}
      <div
        ref={bodyRef}
        className="@container flex-1 overflow-y-auto"
        style={{ overscrollBehavior: "contain", WebkitOverflowScrolling: "touch" } as React.CSSProperties}
      >
        <div data-region="task-head" className="px-4 @min-[560px]:px-6 pt-2 pb-6">
          <h2
            ref={titleRef}
            className="text-xl leading-snug line-clamp-3"
            style={{ color: C.textPrimary, fontWeight: 600 }}
            title={task.title}
          >
            {task.title}
          </h2>
          <TaskStateLine line={stateLine} />
          {stateCard && (
            <TaskStateCard
              card={stateCard}
              task={task}
              onReply={() => {
                selectTab("comments");
                setFocusCommentSignal((n) => n + 1);
              }}
              onOpenLog={() => selectTab("timeline")}
            />
          )}
          {reviewPr && (
            <a
              href={task.pr_url!}
              target="_blank"
              rel="noopener noreferrer"
              data-testid="task-review-pr"
              className="mt-2 inline-flex items-center gap-1 min-h-11 text-sm underline underline-offset-4 cursor-pointer"
              style={{ color: C.textPrimary, textDecorationColor: C.borderActive }}
            >
              {task.pr_number ? t("prChipNumber", { number: task.pr_number }) : t("prChipOpen")}
              <ExternalLink size={14} aria-hidden />
            </a>
          )}
          {showActions && (
            <div className="mt-4">
              <TaskActions task={task} boardId={boardId} />
            </div>
          )}
        </div>

        {/* Tabs — one strip at every width, sticky under the context bar */}
        <div
          className="sticky top-0 z-10 flex gap-1 px-2 scroll-px-2 @min-[560px]:px-4 @min-[560px]:scroll-px-4 tab-strip"
          style={{ background: "var(--detail-bg, var(--color-bg-surface))", borderBottom: `1px solid ${C.border}` }}
          role="tablist"
          aria-label={t("detail.sectionSelect")}
        >
          {tabs.map((x) => {
            const active = activeTab === x.key;
            const count = x.key === "comments" && commentCount > 0 ? commentCount : null;
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
                className="shrink-0 h-11 px-2 text-sm whitespace-nowrap cursor-pointer transition-colors -mb-px"
                style={{
                  color: active ? C.textPrimary : C.textMuted,
                  fontWeight: 500,
                  borderBottom: `2px solid ${active ? C.accent : "transparent"}`,
                }}
              >
                {x.label}
                {count != null && (
                  <span aria-hidden className="ml-1 tabular-nums" style={{ color: C.textMuted }}>{count}</span>
                )}
              </button>
            );
          })}
        </div>

        <div
          className="px-4 @min-[560px]:px-6 py-4 pb-6"
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
              properties={properties}
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
