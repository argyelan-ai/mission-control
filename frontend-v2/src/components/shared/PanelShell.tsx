"use client";

/**
 * PanelShell — the one grammar for everything that opens (13.09.2026):
 *
 *   [Title] [one quiet explaining line]                   [×]
 *   [content — PanelSection titles in sentence case, no mono eyebrows, no rules]
 *   [footer: hint left · Cancel · one primary action right]
 *
 * Modelled on the Add-runtime modal, which already had the calm version.
 * Modal/sheet containers (ResponsiveModal, SlideOverPanel, TaskDetailPanel)
 * keep their own positioning; they render these three parts inside.
 */

import type { ReactNode } from "react";
import { X } from "lucide-react";
import { C } from "@/lib/colors";

export function PanelHeader({
  title,
  description,
  onClose,
  closeLabel = "Close",
  actions,
  compact = false,
  titleId,
}: {
  title: ReactNode;
  /** Set when the dialog uses aria-labelledby. */
  titleId?: string;
  description?: ReactNode;
  onClose?: () => void;
  closeLabel?: string;
  /** Small controls left of the close button (segment switch, ⋯ menu). */
  actions?: ReactNode;
  compact?: boolean;
}) {
  return (
    <div
      className={`flex items-start justify-between gap-4 shrink-0 ${compact ? "px-4 py-3" : "px-5 py-4"}`}
      style={{ borderBottom: `1px solid ${C.border}` }}
    >
      <div className="min-w-0">
        <h2 id={titleId} className="text-[15px] font-semibold leading-snug" style={{ color: C.textPrimary }}>
          {title}
        </h2>
        {description && (
          <p className="text-xs mt-0.5" style={{ color: C.textMuted }}>
            {description}
          </p>
        )}
      </div>
      <div className="flex items-center gap-1.5 shrink-0">
        {actions}
        {onClose && (
          <button
            type="button"
            onClick={onClose}
            aria-label={closeLabel}
            className="w-8 h-8 rounded-md flex items-center justify-center cursor-pointer transition-colors hover:bg-[var(--color-bg-hover)]"
            style={{ color: C.textSecondary }}
          >
            <X size={15} />
          </button>
        )}
      </div>
    </div>
  );
}

/** A titled group inside a panel — sentence case, no rule line. */
export function PanelSection({
  title,
  trailing,
  children,
}: {
  title: ReactNode;
  trailing?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="flex flex-col gap-2.5">
      <div className="flex items-baseline justify-between gap-3">
        <h3 className="text-[13px] font-medium" style={{ color: C.textSecondary }}>
          {title}
        </h3>
        {trailing}
      </div>
      {children}
    </section>
  );
}

export function PanelFooter({
  hint,
  children,
  compact = false,
}: {
  /** Quiet hint on the left ("Cmd+Enter = create · Esc = close"). */
  hint?: ReactNode;
  /** Buttons, right-aligned: Cancel first, the primary action last. */
  children: ReactNode;
  compact?: boolean;
}) {
  return (
    <div
      className={`flex items-center justify-between gap-4 shrink-0 ${compact ? "px-4 py-3" : "px-5 py-4"}`}
      style={{ borderTop: `1px solid ${C.border}` }}
    >
      <div className="text-xs min-w-0 truncate" style={{ color: C.textDim }}>
        {hint}
      </div>
      <div className="flex items-center gap-2 shrink-0">{children}</div>
    </div>
  );
}
