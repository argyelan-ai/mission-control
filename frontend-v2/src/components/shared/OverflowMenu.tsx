"use client";

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Loader2, MoreHorizontal, type LucideIcon } from "lucide-react";
import { C, STATUS_TEXT } from "@/lib/colors";

/**
 * Secondary actions behind one trigger.
 *
 * Runtime cards used to show every action at once (start, stop, restart, wake,
 * re-probe, context settings, recipe switch) as 28x28 icon buttons, most of them
 * disabled and invisible. Primary action stays on the row; the rest lives here.
 *
 * Rendered through a portal with fixed positioning: the cards set
 * `overflow: hidden`, which would clip an absolutely positioned menu.
 */

export interface OverflowAction {
  id: string;
  label: string;
  icon: LucideIcon;
  onClick: () => void;
  disabled?: boolean;
  loading?: boolean;
  /** Renders the label in the error tone. Use only for destructive actions. */
  destructive?: boolean;
}

export function OverflowMenu({
  actions,
  label,
  testId,
}: {
  actions: OverflowAction[];
  label: string;
  testId?: string;
}) {
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);

  const visible = actions.filter((a) => !a.disabled || a.loading);
  const items = visible.length > 0 ? visible : actions;

  useLayoutEffect(() => {
    if (!open || !triggerRef.current) return;
    const r = triggerRef.current.getBoundingClientRect();
    const width = 208;
    const left = Math.min(Math.max(8, r.right - width), window.innerWidth - width - 8);
    const estimated = items.length * 34 + 12;
    const below = r.bottom + 6;
    const top = below + estimated > window.innerHeight - 8 ? Math.max(8, r.top - estimated - 6) : below;
    setPos({ top, left });
  }, [open, items.length]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setOpen(false);
        triggerRef.current?.focus();
      }
    };
    const onPointer = (e: PointerEvent) => {
      const target = e.target as Node;
      if (menuRef.current?.contains(target) || triggerRef.current?.contains(target)) return;
      setOpen(false);
    };
    const close = () => setOpen(false);
    document.addEventListener("keydown", onKey);
    document.addEventListener("pointerdown", onPointer);
    window.addEventListener("resize", close);
    // Cards live in a scrolling container; a fixed menu would drift away from it.
    window.addEventListener("scroll", close, true);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("pointerdown", onPointer);
      window.removeEventListener("resize", close);
      window.removeEventListener("scroll", close, true);
    };
  }, [open]);

  if (actions.length === 0) return null;

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={label}
        title={label}
        data-testid={testId}
        className="flex items-center justify-center w-11 h-11 sm:w-7 sm:h-7 min-w-11 sm:min-w-[28px] rounded-md cursor-pointer transition-colors"
        style={{
          background: open ? C.bgHover : "transparent",
          border: `1px solid ${open ? C.borderActive : "transparent"}`,
          color: open ? C.textPrimary : C.textMuted,
        }}
      >
        <MoreHorizontal size={14} />
      </button>

      {open &&
        pos &&
        typeof document !== "undefined" &&
        createPortal(
          <div
            ref={menuRef}
            role="menu"
            aria-label={label}
            className="fixed z-50 rounded-lg py-1.5 shadow-lg"
            style={{
              top: pos.top,
              left: pos.left,
              width: 208,
              background: C.bgElevated,
              border: `1px solid ${C.borderActive}`,
            }}
          >
            {items.map((a) => {
              const Icon = a.icon;
              return (
                <button
                  key={a.id}
                  role="menuitem"
                  type="button"
                  disabled={a.disabled || a.loading}
                  onClick={() => {
                    setOpen(false);
                    a.onClick();
                  }}
                  className="flex items-center gap-2.5 w-full px-3 py-2.5 min-h-11 sm:min-h-0 sm:py-2 text-xs text-left cursor-pointer transition-colors hover:bg-[var(--color-bg-hover)] disabled:cursor-not-allowed"
                  style={{
                    color: a.disabled
                      ? C.textDim
                      : a.destructive
                        ? STATUS_TEXT.error
                        : C.textSecondary,
                  }}
                >
                  {a.loading ? (
                    <Loader2 size={13} className="animate-spin shrink-0" />
                  ) : (
                    <Icon size={13} className="shrink-0" />
                  )}
                  <span className="truncate">{a.label}</span>
                </button>
              );
            })}
          </div>,
          document.body,
        )}
    </>
  );
}
