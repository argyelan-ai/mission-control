"use client";

/**
 * TaskDetailMenus — the portaled dropdowns of the task detail (status, ⋯,
 * property cells). Extracted from TaskDetailBody (09/2026 cockpit) so both the
 * header and the Technical tab can use them without a circular import.
 */

import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useTranslations } from "next-intl";
import { AnimatePresence, motion } from "framer-motion";
import { Check, ChevronDown, MoreHorizontal, Trash2 } from "lucide-react";
import { C, LANE, STATUS_TEXT } from "@/lib/colors";
import type { TaskStatus } from "@/lib/types";

// ── Status vocabulary ────────────────────────────────────────────────────────

// Message keys in the tasks.* namespace — t() at the render site.
export const STATUS_LABEL_KEY: Record<TaskStatus, string> = {
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
export function usePortalMenu({
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

export function StatusMenu({
  status,
  onChange,
  pending,
  label,
}: {
  status: TaskStatus;
  onChange: (s: TaskStatus) => void;
  pending: boolean;
  /** Trigger text override — the cockpit shows the human-language status here
   *  ("Waiting for your answer") while the menu keeps the technical names. */
  label?: string;
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
        {label ?? t(STATUS_LABEL_KEY[status])}
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

export function OverflowMenu({
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
                    className="px-2 py-1 rounded-sm text-[10px] font-semibold cursor-pointer"
                    style={{ backgroundColor: `${C.error}26`, color: STATUS_TEXT.error }}
                  >
                    {deleteLoading ? "…" : t("deleteTask")}
                  </button>
                  <button
                    onClick={() => {
                      setConfirm(false);
                      setOpen(false);
                    }}
                    className="px-2 py-1 rounded-sm text-[10px] cursor-pointer"
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

export function PropertyMenuCell({
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
