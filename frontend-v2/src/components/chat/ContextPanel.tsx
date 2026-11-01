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
 * on the ring from md up. Rendering it twice would duplicate every label in
 * the accessibility tree for no gain.
 *
 * The popover lives in a portal at the end of <body> (lib/anchoredPopover):
 * as a child of the chat column it was cut off by the column's
 * `overflow-hidden` whenever the side panel made the column narrow. The ring's
 * coordinates arrive as CSS variables that only the `md:` classes read, so the
 * phone sheet keeps its own classes untouched.
 *
 * Segment tones are the documented chart ladder — brightness steps, no hue.
 * The consumed buckets carry the brightness because they are the measurement;
 * "Frei" is the quiet track. (A brief note asked for the accent on "Frei";
 * that would make the absence of usage the loudest mark on the panel, against
 * the Signal rule that emphasis follows meaning, so the ladder is inverted
 * here on purpose.)
 */
import { useEffect, useRef, type CSSProperties, type RefObject } from "react";
import { useLocale, useTranslations } from "next-intl";
import { C, alpha } from "@/lib/colors";
import { formatCompactTokens } from "@/lib/claudeCommands";
import type { SessionCost, UsageComponents, UsageEvent } from "@/lib/chatTypes";
import { PopoverPortal, useAnchoredPosition } from "@/lib/anchoredPopover";

interface Segment {
  /** Stable row id — also the lookup key under `sessions.contextPanelSegment`
   *  (never a translated string here; the render site resolves the label via
   *  `t()`, same convention as `labelKey` elsewhere — see docs/i18n.md). */
  key: string;
  tokens: number;
  color: string;
}

/** Rows in reading order: the three input-side buckets, then output, then the
 *  remainder. `free` is appended by the caller once the window is known. */
export function buildSegments(components: UsageComponents): Segment[] {
  return [
    { key: "input", tokens: components.input, color: C.chart.cpu },
    { key: "cacheRead", tokens: components.cacheRead, color: C.accentDeep },
    { key: "cacheCreation", tokens: components.cacheCreation, color: C.chart.ram },
    { key: "output", tokens: components.output, color: C.chart.disk },
  ];
}

/** Prompt cache hit rate for the latest turn: cached / (cached + uncached
 *  input). `cacheCreation` (freshly written this turn, not yet a hit) is
 *  deliberately left out of both sides — the same convention Insights' own
 *  `cacheHitRate` uses, so the two figures never disagree. `null` whenever a
 *  harness has no breakdown, or both buckets are empty — never a fake 0%. */
export function cacheHitRateOf(components: UsageComponents | null | undefined): number | null {
  if (!components) return null;
  const { cacheRead, input } = components;
  const denom = cacheRead + input;
  return denom > 0 ? cacheRead / denom : null;
}

export function usedTokensOf(usage: UsageEvent): number {
  const c = usage.components;
  if (c) return c.input + c.cacheRead + c.cacheCreation + c.output;
  // No breakdown: `inputTokens` is already the input-side sum the ring's own
  // fallback estimate uses, so the two views can't disagree.
  return usage.inputTokens;
}

/** The session's cost as the harness reported it, in the active locale —
 *  or `null` when there is nothing real to show (absent, zero, negative or
 *  not a number: a local model reports nothing, and "0.00" would be a
 *  figure nobody measured). Small amounts keep four decimals so a cheap
 *  session does not read as free. An unknown currency code falls back to
 *  the plain number plus the code rather than throwing. */
export function formatSessionCost(cost: SessionCost | null | undefined, locale: string): string | null {
  if (!cost || typeof cost.amount !== "number" || !Number.isFinite(cost.amount) || cost.amount <= 0) {
    return null;
  }
  const digits = cost.amount < 1 ? 4 : 2;
  try {
    return new Intl.NumberFormat(locale, {
      style: "currency",
      currency: cost.currency || "USD",
      minimumFractionDigits: 2,
      maximumFractionDigits: digits,
    }).format(cost.amount);
  } catch {
    return `${cost.amount.toFixed(digits)} ${cost.currency}`;
  }
}

interface ContextPanelProps {
  usage: UsageEvent;
  /** The same percentage the ring shows, so the two never disagree. */
  pct: number | null;
  pctSource: "cli" | "estimate" | null;
  onClose: () => void;
  /** The ring the desktop popover hangs on. Without it the box sits at the
   *  viewport's top-left corner — only tests render it that way. */
  anchorRef?: RefObject<HTMLElement | null>;
}

const NO_ANCHOR: RefObject<HTMLElement | null> = { current: null };

export function ContextPanel({ usage, pct, pctSource, onClose, anchorRef = NO_ANCHOR }: ContextPanelProps) {
  const panelRef = useRef<HTMLDivElement>(null);
  const anchored = useAnchoredPosition(true, anchorRef, panelRef, { align: "start" });
  const t = useTranslations("sessions");
  const locale = useLocale();

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
    : [{ key: "used", tokens: used, color: C.chart.cpu }];

  const rows: Segment[] =
    free != null
      ? [...usedSegments, { key: "free", tokens: free, color: C.bgHover }]
      : usedSegments;

  // Bar shares come from the window when we know it, otherwise from the used
  // total — a bar without a denominator would be decoration.
  const barTotal = window_ ?? used;
  const share = (tokens: number) => (barTotal > 0 ? (tokens / barTotal) * 100 : 0);
  const segmentLabel = (key: string) => t(`contextPanelSegment.${key}`);
  // `cacheHitRate` (backend-computed, survives the components-null case —
  // see its doc comment in chatTypes.ts) wins; `components`-derived stays
  // the fallback for older event shapes that never set the new field.
  const cacheRate = usage.cacheHitRate ?? cacheHitRateOf(usage.components);
  // Percent formatting goes through the active locale (K3/i18n): "15%" in
  // English, "15 %" with a narrow no-break space in German — never a bare
  // `Math.round(...) + "%"` string concat.
  const cost = formatSessionCost(usage.sessionCost, locale);
  const formatPct = (value: number, fractionDigits: number) =>
    new Intl.NumberFormat(locale, { style: "percent", maximumFractionDigits: fractionDigits }).format(
      value / 100,
    );

  // Desktop coordinates as CSS variables (see the file comment); until the
  // first measurement the desktop box stays invisible instead of flashing at 0/0.
  const anchorVars = {
    "--anchor-top": `${anchored?.top ?? 0}px`,
    "--anchor-left": `${anchored?.left ?? 0}px`,
    "--anchor-max-h": `${anchored?.maxHeight ?? 0}px`,
  } as CSSProperties;

  return (
    <PopoverPortal>
      {/* Mobile-only scrim. Anchored below the app bar for the same stacking
          reason as the other sheets (see --mobile-chat-topbar-h). */}
      <div
        className="fixed inset-x-0 bottom-0 top-[var(--mobile-chat-topbar-h)] z-40 md:hidden"
        style={{ background: alpha(C.scrim, 0.75) }}
        aria-hidden="true"
      />
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="false"
        aria-label={t("contextPanelTitle")}
        data-testid="context-panel"
        className={`fixed inset-x-0 bottom-0 z-50 px-4 pt-3 pb-safe md:inset-auto md:top-(--anchor-top) md:left-(--anchor-left) md:max-h-(--anchor-max-h) md:overflow-y-auto md:w-[300px] md:max-w-[320px] md:px-3 md:py-3 md:pb-3${anchored ? "" : " md:invisible"}`}
        // One radius on all four corners: as a desktop popover the box is fully
        // visible, and rounding only the top read as a mismatch. On mobile the
        // bottom corners sit off-screen, so the same value serves both.
        style={{
          ...anchorVars,
          background: C.bgElevated,
          borderRadius: "var(--radius-xl)",
          borderTop: `2px solid ${C.accent}`,
          boxShadow: "var(--shadow-elevated)",
        }}
      >
        <div className="flex items-baseline justify-between gap-2 mb-2.5">
          <span className="text-[14px] font-semibold" style={{ color: C.textPrimary }}>
            {t("contextPanelTitle")}
          </span>
          {pct != null && (
            <span
              className="font-mono text-[13px] font-medium tabular-nums"
              data-testid="context-panel-pct"
              style={{ color: C.textSecondary }}
            >
              {formatPct(pct, 0)}
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
                {segmentLabel(s.key)}
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
                  {formatPct(share(s.tokens), 1)}
                </span>
              )}
            </div>
          ))}
        </div>

        <div className="mt-3 pt-2.5 flex flex-col gap-1" style={{ borderTop: `1px solid ${C.borderSubtle}` }}>
          <div className="flex items-center justify-between gap-2 text-xs font-medium" style={{ color: C.textMuted }}>
            <span>{t("contextPanelWindowTotal")}</span>
            <span className="font-mono tabular-nums" style={{ color: C.textSecondary }}>
              {window_ != null ? formatCompactTokens(window_) : t("contextPanelWindowUnknown")}
            </span>
          </div>
          <div className="flex items-center justify-between gap-2 text-xs font-medium" style={{ color: C.textMuted }}>
            <span>{t("contextPanelSource")}</span>
            <span className="font-mono" data-testid="context-panel-source" style={{ color: C.textSecondary }}>
              {/* "CLI" is the same word in both languages (see Composer.tsx's
                  ringTitle comment), so it stays a literal — only the
                  estimate alternative is translated. */}
              {pctSource === "estimate" ? t("contextSourceEstimate") : pctSource === "cli" ? "CLI" : "—"}
            </span>
          </div>
          {cacheRate != null && (
            <div className="flex items-center justify-between gap-2 text-xs font-medium" style={{ color: C.textMuted }}>
              <span>{t("contextPanelCacheRate")}</span>
              <span
                className="font-mono tabular-nums"
                data-testid="context-panel-cache-rate"
                style={{ color: C.textSecondary }}
              >
                {formatPct(cacheRate * 100, 0)}
              </span>
            </div>
          )}
          {cost && (
            <div className="flex items-center justify-between gap-2 text-xs font-medium" style={{ color: C.textMuted }}>
              <span>{t("contextPanelCost")}</span>
              <span
                className="font-mono tabular-nums"
                data-testid="context-panel-cost"
                style={{ color: C.textSecondary }}
              >
                {cost}
              </span>
            </div>
          )}
          <p className="text-[12px] leading-[1.55] mt-1" style={{ color: C.textMuted }}>
            {t("contextPanelNote")}
          </p>
        </div>

        <button
          type="button"
          onClick={onClose}
          className="md:hidden mt-3 w-full min-h-touch text-[13px] font-medium rounded-lg cursor-pointer"
          style={{ color: C.textSecondary, border: `1px solid ${C.borderActive}` }}
        >
          {t("contextPanelClose")}
        </button>
      </div>
    </PopoverPortal>
  );
}
