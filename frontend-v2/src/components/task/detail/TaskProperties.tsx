"use client";

/**
 * Properties of a task as a calm list (DESIGN.md K12): field name left in the
 * muted meta tone, value right, one hairline between rows, 44 px per row. On
 * the phone it sits under the brief in the Summary tab; from 720 px container
 * width it is the right column (TaskSummaryTab places it). Replaces the old
 * tile grid of facts under the state card.
 */

import { C } from "@/lib/colors";

export function PropRow({ label, testId, children }: { label: string; testId?: string; children: React.ReactNode }) {
  return (
    <div
      data-testid={testId}
      className="flex items-center gap-3 min-h-11"
      style={{ borderBottom: `1px solid ${C.borderSubtle}` }}
    >
      <span className="w-28 shrink-0 text-sm" style={{ color: C.textMuted }}>{label}</span>
      <div className="flex-1 min-w-0 flex items-center gap-2 text-sm" style={{ color: C.textPrimary }}>
        {children}
      </div>
    </div>
  );
}

export function TaskProperties({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section data-region="task-props" data-testid="task-properties" aria-label={title}>
      <h3 className="label-sys mb-2">{title}</h3>
      <div className="flex flex-col">{children}</div>
    </section>
  );
}
