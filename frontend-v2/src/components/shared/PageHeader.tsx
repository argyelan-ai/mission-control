"use client";

/**
 * PageHeader — the one header grammar for every page (13.09.2026):
 *
 *   [Title]  [quiet meta]                       [one primary action]
 *   [optional tabs — text + underline, one form]
 *
 * Deliberately absent: the mono eyebrow ("CONSOLE · TASKS" — the sidebar
 * already says where you are), the rule line under the title (on paper,
 * spacing separates), decorative icons before the title. Pages that had
 * their own header markup (seven variants counted on 13.09.) route through
 * this component; the greeting on Home is just a title like any other.
 */

import type { ReactNode } from "react";
import { C } from "@/lib/colors";

export function PageHeader({
  title,
  meta,
  actions,
  tabs,
  compact = false,
}: {
  title: ReactNode;
  /** Quiet context next to the title: "4 open", "15/15 online". Colour it only when it asks for action. */
  meta?: ReactNode;
  /** The one primary action (or a small group) on the right. */
  actions?: ReactNode;
  /** A <PageTabs/> row below the title. */
  tabs?: ReactNode;
  /** Tighter vertical rhythm for split views (Tasks list, Sessions). */
  compact?: boolean;
}) {
  return (
    <header className={compact ? "mb-3" : "mb-6"} data-page-header>
      <div className="flex items-center justify-between gap-4 min-w-0">
        <div className="flex items-baseline gap-3 min-w-0">
          <h1
            className={`display font-semibold leading-tight truncate ${compact ? "text-[20px]" : "text-2xl"}`}
            style={{ color: C.textPrimary }}
          >
            {title}
          </h1>
          {meta != null && meta !== "" && (
            <span className="text-[13px] shrink-0" style={{ color: C.textMuted }}>
              {meta}
            </span>
          )}
        </div>
        {actions && <div className="flex items-center gap-2 shrink-0">{actions}</div>}
      </div>
      {tabs && <div className="mt-4">{tabs}</div>}
    </header>
  );
}

/** The one primary/secondary action form for page headers. */
export function PageAction({
  children,
  onClick,
  variant = "primary",
  disabled,
  title,
  ariaLabel,
  testId,
}: {
  children: ReactNode;
  onClick?: () => void;
  variant?: "primary" | "secondary";
  disabled?: boolean;
  title?: string;
  ariaLabel?: string;
  testId?: string;
}) {
  const primary = variant === "primary";
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      title={title}
      aria-label={ariaLabel}
      data-testid={testId}
      className="inline-flex items-center gap-2 px-3.5 py-2 text-sm rounded-lg font-medium cursor-pointer transition-opacity hover:opacity-90 disabled:opacity-50 disabled:cursor-not-allowed shrink-0"
      style={
        primary
          ? { background: C.accent, color: C.onAccent }
          : { background: "transparent", color: C.textSecondary, border: `1px solid ${C.borderActive}` }
      }
    >
      {children}
    </button>
  );
}

export interface PageTabItem<K extends string = string> {
  key: K;
  label: ReactNode;
  count?: number | string | null;
}

/** Text tabs with an underline — the single tab form across pages. */
export function PageTabs<K extends string = string>({
  items,
  active,
  onChange,
  ariaLabel,
}: {
  items: PageTabItem<K>[];
  active: K;
  onChange: (key: K) => void;
  ariaLabel?: string;
}) {
  return (
    <div role="tablist" aria-label={ariaLabel} className="flex gap-1 -mb-px" style={{ borderBottom: `1px solid ${C.border}` }}>
      {items.map((item) => {
        const on = item.key === active;
        return (
          <button
            key={item.key}
            role="tab"
            type="button"
            aria-selected={on}
            onClick={() => onChange(item.key)}
            className="px-3 py-2 text-[13px] cursor-pointer transition-colors min-h-11 sm:min-h-0"
            style={{
              color: on ? C.textPrimary : C.textMuted,
              fontWeight: on ? 500 : 400,
              borderBottom: `2px solid ${on ? C.accent : "transparent"}`,
              marginBottom: -1,
            }}
          >
            {item.label}
            {item.count != null && item.count !== "" && (
              <span className="ml-1.5 font-mono text-[11px]" style={{ color: C.textDim }}>
                {item.count}
              </span>
            )}
          </button>
        );
      })}
    </div>
  );
}
