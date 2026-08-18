"use client";

/**
 * ContextPanel — the detail view behind the composer's context ring, modelled
 * on Claude Code desktop's context breakdown: where the window actually went.
 *
 * The ring answers "how full is it"; this answers "with what". Those are
 * different questions, and cramming the second into a tooltip is why the
 * tooltip had grown into three sentences.
 *
 * One DOM node serves both breakpoints, the way the Diff/Browser panel does:
 * a bottom sheet below md (`fixed inset-x-0 bottom-0`), a popover anchored
 * above the ring from md up (`md:absolute md:bottom-full`). Rendering it twice
 * would duplicate every label in the accessibility tree for no gain.
 *
 * Segment tones are the documented chart ladder — brightness steps, no hue.
 * The consumed buckets carry the brightness because they are the measurement;
 * "Frei" is the quiet track. (A brief note asked for the accent on "Frei";
 * that would make the absence of usage the loudest mark on the panel, against
 * the Signal rule that emphasis follows meaning, so the ladder is inverted
 * here on purpose.)
 */
import { useEffect, useRef } from "react";
import { C } from "@/lib/colors";
import { formatCompactTokens } from "@/lib/claudeCommands";
import type { UsageComponents, UsageEvent } from "@/lib/chatTypes";

interface Segment {
  key: string;
  label: string;
  tokens: number;
  color: string;
}

/** Rows in reading order: the three input-side buckets, then output, then the
 *  remainder. `free` is appended by the caller once the window is known. */
export function buildSegments(components: UsageComponents): Segment[] {
  return [
    { key: "input", label: "Eingabe", tokens: components.input, color: C.chart.cpu },
    { key: "cacheRead", label: "Cache gelesen", tokens: components.cacheRead, color: C.accentDeep },
    { key: "cacheCreation", label: "Cache geschrieben", tokens: components.cacheCreation, color: C.chart.ram },
    { key: "output", label: "Ausgabe", tokens: components.output, color: C.chart.disk },
  ];
}

export function usedTokensOf(usage: UsageEvent): number {
  const c = usage.components;
  if (c) return c.input + c.cacheRead + c.cacheCreation + c.output;
  // No breakdown: `inputTokens` is already the input-side sum the ring's own
  // fallback estimate uses, so the two views can't disagree.
  return usage.inputTokens;
}

interface ContextPanelProps {
  usage: UsageEvent;
  /** The same percentage the ring shows, so the two never disagree. */
  pct: number | null;
  pctSource: "cli" | "estimate" | null;
  onClose: () => void;
}

export function ContextPanel({ usage, pct, pctSource, onClose }: ContextPanelProps) {
  const panelRef = useRef<HTMLDivElement>(null);

  // Escape closes; a click anywhere outside closes. The mobile scrim covers
  // the outside-click case visually, but the listener is what makes the
  // desktop popover behave like every other popover on the platform.
  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") {
        e.stopPropagation();
        onClose();
      }
    }
    function onPointerDown(e: MouseEvent) {
      const el = panelRef.current;
      if (!el) return;
      const target = e.target as Node | null;
      if (target && !el.contains(target) && !(target as HTMLElement).closest?.("[data-context-trigger]")) {
        onClose();
      }
    }
    document.addEventListener("keydown", onKeyDown);
    document.addEventListener("mousedown", onPointerDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.removeEventListener("mousedown", onPointerDown);
    };
  }, [onClose]);

  const window_ = typeof usage.contextWindow === "number" && usage.contextWindow > 0 ? usage.contextWindow : null;
  const used = usedTokensOf(usage);
  const free = window_ != null ? Math.max(window_ - used, 0) : null;

  const usedSegments: Segment[] = usage.components
    ? buildSegments(usage.components).filter((s) => s.tokens > 0)
    : [{ key: "used", label: "Belegt", tokens: used, color: C.chart.cpu }];

  const rows: Segment[] =
    free != null
      ? [...usedSegments, { key: "free", label: "Frei", tokens: free, color: C.bgHover }]
      : usedSegments;

  // Bar shares come from the window when we know it, otherwise from the used
  // total — a bar without a denominator would be decoration.
  const barTotal = window_ ?? used;
  const share = (tokens: number) => (barTotal > 0 ? (tokens / barTotal) * 100 : 0);

  return (
    <>
      {/* Mobile-only scrim. Anchored below the app bar for the same stacking
          reason as the other sheets (see --mobile-appbar-h). */}
      <div
        className="fixed inset-x-0 bottom-0 top-[var(--mobile-appbar-h)] z-40 md:hidden"
        style={{ background: "rgba(10,10,10,0.75)" }}
        aria-hidden="true"
      />
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="false"
        aria-label="Kontext"
        data-testid="context-panel"
        className="fixed inset-x-0 bottom-0 z-50 px-4 pt-3 pb-safe md:absolute md:inset-auto md:bottom-full md:left-0 md:z-30 md:mb-2 md:w-[300px] md:max-w-[320px] md:px-3 md:py-3 md:pb-3"
        // One radius on all four corners: as a desktop popover the box is fully
        // visible, and rounding only the top read as a mismatch. On mobile the
        // bottom corners sit off-screen, so the same value serves both.
        style={{
          background: C.bgElevated,
          borderRadius: "var(--radius-xl)",
          borderTop: `2px solid ${C.accent}`,
          boxShadow: "var(--shadow-elevated)",
        }}
      >
        <div className="flex items-baseline justify-between gap-2 mb-2.5">
          <span className="text-[14px] font-semibold" style={{ color: C.textPrimary }}>
            Kontext
          </span>
          {pct != null && (
            <span
              className="font-mono text-[13px] font-medium tabular-nums"
              data-testid="context-panel-pct"
              style={{ color: C.textSecondary }}
            >
              {Math.round(pct)}%
            </span>
          )}
        </div>

        {/* Stacked bar. Rounded ends on the track only, so the segments read as
            one measured strip rather than a row of pills. */}
        <div
          className="flex h-2 w-full overflow-hidden mb-3"
          style={{ background: C.bgHover, borderRadius: "var(--radius-full)" }}
          data-testid="context-panel-bar"
          aria-hidden="true"
        >
          {usedSegments.map((s) => (
            <div key={s.key} style={{ width: `${share(s.tokens)}%`, background: s.color }} />
          ))}
        </div>

        <div className="flex flex-col gap-1.5">
          {rows.map((s) => (
            <div key={s.key} className="flex items-center gap-2" data-testid={`context-row-${s.key}`}>
              <span
                className="w-2 h-2 rounded-full shrink-0"
                style={{ background: s.color, border: s.key === "free" ? `1px solid ${C.borderActive}` : undefined }}
                aria-hidden="true"
              />
              <span className="flex-1 min-w-0 truncate text-[12px]" style={{ color: C.textSecondary }}>
                {s.label}
              </span>
              <span
                className="font-mono text-xs font-medium tabular-nums shrink-0"
                style={{ color: C.textPrimary }}
              >
                {formatCompactTokens(s.tokens)}
              </span>
              {window_ != null && (
                <span
                  className="font-mono text-xs font-medium tabular-nums shrink-0 w-12 text-right"
                  style={{ color: C.textMuted }}
                >
                  {share(s.tokens).toFixed(1)}%
                </span>
              )}
            </div>
          ))}
        </div>

        <div className="mt-3 pt-2.5 flex flex-col gap-1" style={{ borderTop: `1px solid ${C.borderSubtle}` }}>
          <div className="flex items-center justify-between gap-2 text-xs font-medium" style={{ color: C.textMuted }}>
            <span>Fenster gesamt</span>
            <span className="font-mono tabular-nums" style={{ color: C.textSecondary }}>
              {window_ != null ? formatCompactTokens(window_) : "unbekannt"}
            </span>
          </div>
          <div className="flex items-center justify-between gap-2 text-xs font-medium" style={{ color: C.textMuted }}>
            <span>Quelle</span>
            <span className="font-mono" data-testid="context-panel-source" style={{ color: C.textSecondary }}>
              {pctSource === "estimate" ? "Schätzung" : pctSource === "cli" ? "CLI" : "—"}
            </span>
          </div>
          <p className="text-[12px] leading-[1.55] mt-1" style={{ color: C.textMuted }}>
            Die CLI-Statuszeile zeigt dagegen den Rest bis zur Auto-Komprimierung an — andere Basis,
            beide korrekt.
          </p>
        </div>

        <button
          type="button"
          onClick={onClose}
          className="md:hidden mt-3 w-full min-h-touch text-[13px] font-medium rounded-lg cursor-pointer"
          style={{ color: C.textSecondary, border: `1px solid ${C.borderActive}` }}
        >
          Schliessen
        </button>
      </div>
    </>
  );
}
