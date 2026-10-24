"use client";

// Small building blocks of the test sheet: a field row (name left, value
// right, one shared left edge — K9), a selection chip (≥ 44 px, K11) and a
// note. Colours only from colors.ts; the accent marks selection, status
// tones mark state (K6).
import type { ReactNode } from "react";
import { Check, ChevronRight } from "lucide-react";
import { C, STATUS_TEXT, alpha } from "@/lib/colors";

const rowEdge = { borderBottom: `1px solid ${C.borderSubtle}` };

export function FieldRow({
  name,
  children,
  sub,
  action,
  testId,
}: {
  name: string;
  children: ReactNode;
  sub?: ReactNode;
  /** A trailing control (e.g. remove ✕) — the row itself stays static. */
  action?: ReactNode;
  testId?: string;
}) {
  return (
    <div className="flex items-start gap-3 py-3" style={rowEdge} data-testid={testId}>
      <span className="w-[4.75rem] shrink-0 text-sm pt-3" style={{ color: C.textMuted }}>
        {name}
      </span>
      <div className="flex-1 min-w-0 pt-2">
        {children}
        {sub && (
          <div className="text-sm mt-1" style={{ color: C.textMuted }}>
            {sub}
          </div>
        )}
      </div>
      {action}
    </div>
  );
}

/** A whole row as one button (Model, Test): name, value, sub, chevron. */
export function TapRow({
  name,
  main,
  sub,
  onClick,
  ariaLabel,
  testId,
}: {
  name: string;
  main: ReactNode;
  sub?: ReactNode;
  onClick: () => void;
  ariaLabel: string;
  testId?: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={ariaLabel}
      data-testid={testId}
      className="w-full flex items-center gap-3 py-3 min-h-touch text-left cursor-pointer"
      style={rowEdge}
    >
      <span className="w-[4.75rem] shrink-0 text-sm" style={{ color: C.textMuted }}>
        {name}
      </span>
      <span className="flex-1 min-w-0">
        <span className="block text-base font-medium break-words" style={{ color: C.textPrimary }}>
          {main}
        </span>
        {sub && (
          <span className="block text-sm mt-1" style={{ color: C.textMuted }}>
            {sub}
          </span>
        )}
      </span>
      <ChevronRight size={18} aria-hidden style={{ color: C.textDim }} className="shrink-0" />
    </button>
  );
}

export function ChipGroup({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div role="group" aria-label={label} className="flex flex-wrap gap-2">
      {children}
    </div>
  );
}

export function Chip({
  on,
  onClick,
  disabled,
  check,
  children,
  testId,
}: {
  on: boolean;
  onClick?: () => void;
  disabled?: boolean;
  /** Multi-select: a check mark in front of a chosen chip. */
  check?: boolean;
  children: ReactNode;
  testId?: string;
}) {
  return (
    <button
      type="button"
      aria-pressed={on}
      disabled={disabled}
      onClick={onClick}
      data-testid={testId}
      className="inline-flex items-center gap-2 min-h-11 px-4 rounded-full text-sm cursor-pointer transition-colors disabled:cursor-not-allowed"
      style={{
        background: on ? C.accentSubtle : "transparent",
        border: `1px solid ${on ? C.borderAccent : C.borderActive}`,
        // One weight for both states: a chosen chip must not grow and push
        // its neighbours into a new line.
        color: disabled ? C.textDim : on ? C.textPrimary : C.textSecondary,
      }}
    >
      {check && on && <Check size={15} aria-hidden />}
      {children}
    </button>
  );
}

export type NoteTone = "plain" | "info" | "warn" | "error";

const TONE: Record<NoteTone, string> = {
  plain: C.textSecondary,
  info: C.info,
  warn: C.warning,
  error: C.error,
};

/** A note the operator should read before Start (blocker, class mix, probe
 *  failed). Only notes that ask for attention get a surface (K8). */
export function Note({
  tone,
  icon,
  title,
  children,
  testId,
  className = "mt-4",
}: {
  tone: NoteTone;
  icon: ReactNode;
  title?: ReactNode;
  children?: ReactNode;
  testId?: string;
  className?: string;
}) {
  const color = TONE[tone];
  const textColor =
    tone === "warn" ? STATUS_TEXT.warning : tone === "error" ? STATUS_TEXT.error : tone === "info" ? STATUS_TEXT.info : C.textSecondary;
  return (
    <div
      className={`flex gap-3 rounded-lg px-4 py-3 text-sm ${className}`}
      data-testid={testId}
      style={{
        background: tone === "plain" ? "transparent" : alpha(color, 0.08),
        border: `1px solid ${tone === "plain" ? C.border : alpha(color, 0.3)}`,
        color: C.textSecondary,
      }}
    >
      <span className="shrink-0 pt-px" style={{ color: textColor }} aria-hidden>
        {icon}
      </span>
      <div className="min-w-0">
        {title && (
          <div className="font-medium" style={{ color: tone === "plain" ? C.textPrimary : textColor }}>
            {title}
          </div>
        )}
        {children && <div className={title ? "mt-1" : undefined}>{children}</div>}
      </div>
    </div>
  );
}
