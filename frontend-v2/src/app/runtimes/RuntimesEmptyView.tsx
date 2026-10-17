"use client";

/**
 * RuntimesEmptyView — what a page tab shows when it has nothing to show yet.
 *
 * A fresh install has no machines, so the Fleet tab used to be a blank area
 * with no way forward. This view says in one sentence what belongs on the tab
 * and offers the EXISTING ways to add it (ADR-093: no second path) — the
 * caller wires the buttons to the "Add device" chooser, the "+" add-runtime
 * dialog or the provider catalog. No card, no illustration (K8): it sits on
 * the tab bar's left edge (K9) with one primary action (K11), styled like the page's
 * other add buttons — the app bar's "New" already owns the filled accent.
 */

import { C } from "@/lib/colors";

export interface EmptyAction {
  label: string;
  onClick: () => void;
}

export function RuntimesEmptyView({
  region,
  text,
  primary,
  secondary,
}: {
  region: "fleet" | "cloud";
  text: string;
  primary?: EmptyAction;
  secondary?: EmptyAction;
}) {
  return (
    <div
      data-testid={`runtimes-empty-${region}`}
      data-region={`runtimes-empty-${region}`}
      className="flex flex-col items-start gap-4 py-2"
    >
      <p className="text-sm max-w-prose" style={{ color: C.textSecondary }}>
        {text}
      </p>
      {(primary || secondary) && (
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
          {primary && (
            <button
              type="button"
              onClick={primary.onClick}
              className="text-xs px-3.5 py-2.5 min-h-11 rounded-md cursor-pointer transition-colors"
              style={{ background: C.accentSubtle, border: `1px solid ${C.borderAccent}`, color: C.accent }}
            >
              {primary.label}
            </button>
          )}
          {secondary && (
            <button
              type="button"
              onClick={secondary.onClick}
              className="text-xs min-h-11 cursor-pointer underline underline-offset-4"
              style={{ color: C.textMuted }}
            >
              {secondary.label}
            </button>
          )}
        </div>
      )}
    </div>
  );
}
