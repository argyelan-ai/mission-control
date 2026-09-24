"use client";

/**
 * On/off switch with a 44 × 44 px hit area (PRINCIPLES §7.10) around the
 * usual 36 × 20 track — same look as the settings toggles.
 */

import { C } from "@/lib/colors";

export function NightSwitch({
  checked,
  onChange,
  label,
  disabled = false,
  testId,
  describedBy,
}: {
  checked: boolean;
  onChange: (next: boolean) => void;
  label: string;
  disabled?: boolean;
  testId?: string;
  describedBy?: string;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      aria-describedby={describedBy}
      data-testid={testId}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className="shrink-0 inline-flex items-center justify-center min-h-[44px] min-w-[44px] cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
    >
      <span
        aria-hidden
        className="relative block rounded-full transition-colors"
        style={{
          width: 36,
          height: 20,
          backgroundColor: checked ? C.accent : "var(--color-bg-elevated)",
          border: `1px solid ${checked ? C.accent : "var(--color-border)"}`,
        }}
      >
        <span
          className="absolute top-1/2 -translate-y-1/2 rounded-full transition-all"
          style={{
            left: checked ? 18 : 2,
            width: 14,
            height: 14,
            backgroundColor: checked ? "var(--color-on-accent)" : "var(--color-text-muted)",
          }}
        />
      </span>
    </button>
  );
}
