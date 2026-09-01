"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";
import { ChevronDown } from "lucide-react";
import { C, STATUS_TEXT } from "@/lib/colors";
import { cn } from "@/lib/utils";

/**
 * The one list grammar for /runtimes — with a mobile anatomy of its own.
 *
 * The page had grown eight row components (runtimes, LM Studio models, hosts,
 * CLI tools, catalog providers, catalog models, local recipes, discovered
 * containers) and roughly a dozen chip styles across three radii, three text
 * sizes and three padding scales. Every section had invented its own row.
 *
 * Desktop is one line:
 *
 *   [status dot] name [meta chips] ......... [one primary action] [overflow]
 *
 * Mobile is NOT that line reflowed. Letting the chips wrap produced rows from
 * 38 to 154 px tall, six chips over five lines, and no two rows the same
 * height — a squeezed desktop, which is exactly what it looked like. At 390 px
 * a row is two fixed lines instead:
 *
 *   [status dot] name .................... [action] [overflow]   (44px targets)
 *   one-line summary — the single fact worth reading ........... [expand]
 *
 * Everything else (the full chip set, identifiers, live status, bound agents)
 * lives behind the expand toggle. The row therefore has ONE height until the
 * operator asks for more.
 */

/**
 * The only tones on the page.
 *
 * `ok` / `warn` / `error` / `idle` are the four state tones — colour here always
 * means state, never decoration. `accent` is deliberately achromatic (#EBE8DE):
 * it carries emphasis through brightness, so it can mark "new / needs your
 * attention" without adding a fifth hue to the vocabulary.
 */
export type Tone = "ok" | "warn" | "error" | "idle" | "accent";

const DOT: Record<Tone, string> = {
  ok: C.online,
  warn: C.warning,
  error: C.error,
  idle: "#3A3A3A",
  accent: C.accent,
};

const CHIP: Record<Tone, { color: string; border: string; background: string }> = {
  ok: { color: STATUS_TEXT.online, border: `${C.online}40`, background: "transparent" },
  warn: { color: STATUS_TEXT.warning, border: `${C.warning}40`, background: "transparent" },
  error: { color: STATUS_TEXT.error, border: `${C.error}40`, background: "transparent" },
  idle: { color: C.textMuted, border: C.borderActive, background: "transparent" },
  accent: { color: C.accent, border: C.borderAccent, background: C.accentSubtle },
};

/**
 * One chip size, one radius, one type step, page-wide.
 *
 * Chips are for categorical facts (state, type, quantisation, architecture).
 * Identifiers and free text belong in `meta`, not in a chip.
 */
export function MetaChip({
  tone = "idle",
  icon,
  title,
  className,
  testId,
  /** For subjects that are present but unavailable (a busy agent). */
  dimmed,
  children,
}: {
  tone?: Tone;
  icon?: React.ReactNode;
  title?: string;
  className?: string;
  testId?: string;
  dimmed?: boolean;
  children: React.ReactNode;
}) {
  const c = CHIP[tone];
  return (
    <span
      title={title}
      data-testid={testId}
      className={cn(
        "shrink-0 inline-flex items-center gap-1 label-sys rounded-sm px-1.5 py-0.5 leading-none",
        className,
      )}
      style={{
        color: c.color,
        border: `1px solid ${c.border}`,
        background: c.background,
        ...(dimmed ? { opacity: 0.45 } : {}),
      }}
    >
      {icon}
      {children}
    </span>
  );
}

/**
 * The one labelled action button on a row.
 *
 * Deploy, Create as runtime, Update, Add and Load each had their own padding,
 * radius, tracking and font family — three geometries across two families for
 * the same job. 44px tall on a thumb, 28px once a pointer is aiming.
 */
export function RowAction({
  tone = "accent",
  icon,
  onClick,
  disabled,
  title,
  testId,
  children,
}: {
  tone?: Tone;
  icon?: React.ReactNode;
  onClick: () => void;
  disabled?: boolean;
  title?: string;
  testId?: string;
  children: React.ReactNode;
}) {
  const c = CHIP[tone];
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      title={title}
      data-testid={testId}
      className="shrink-0 inline-flex items-center justify-center gap-1 rounded-md px-2.5 min-h-11 sm:min-h-7 label-sys cursor-pointer transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
      style={{ color: c.color, border: `1px solid ${c.border}`, background: c.background }}
    >
      {icon}
      {children}
    </button>
  );
}

/** Plain secondary text inside a row's meta line (identifiers, endpoints). */
export function MetaText({
  mono,
  title,
  className,
  children,
}: {
  mono?: boolean;
  title?: string;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <span
      title={title}
      className={cn("min-w-0 truncate text-[11px]", mono && "font-mono", className)}
      style={{ color: C.textMuted }}
    >
      {children}
    </span>
  );
}

export function ListRow({
  tone = "idle",
  name,
  nameSuffix,
  chips,
  meta,
  detail,
  summary,
  action,
  overflow,
  leading,
  muted,
  onClick,
  testId,
  dataAttrs,
  className,
}: {
  /** Drives the status dot. */
  tone?: Tone;
  name: React.ReactNode;
  /** Tightly bound to the name (key icon, lock). */
  nameSuffix?: React.ReactNode;
  /** MetaChips, in the page-wide order: state → type → size/detail. */
  chips?: React.ReactNode;
  /** Free text after the chips (identifiers, endpoints). */
  meta?: React.ReactNode;
  /** Second line for things that need a sentence (live status, agents). */
  detail?: React.ReactNode;
  /**
   * The one fact worth reading at 390px. Shown instead of the chips on mobile;
   * without it a row falls back to wrapping chips, which is the thing this
   * component exists to prevent.
   */
  summary?: React.ReactNode;
  /** Exactly one primary action. Everything else goes in `overflow`. */
  action?: React.ReactNode;
  overflow?: React.ReactNode;
  /** Rendered before the status dot — only for disclosure chevrons. */
  leading?: React.ReactNode;
  muted?: boolean;
  onClick?: () => void;
  testId?: string;
  dataAttrs?: Record<string, string>;
  className?: string;
}) {
  const t = useTranslations("common.listRow");
  const [expanded, setExpanded] = useState(false);
  const hasMore = Boolean(chips || meta || detail);
  const showAccessory = hasMore && !onClick;

  const head = (
    // min-h-11 on mobile so every row is the same height whether or not it
    // carries an action button; 38px from sm up where a pointer is aiming.
    <div className="flex items-center gap-2 min-h-11 sm:min-h-[38px]">
      {leading}
      <span
        aria-hidden
        className="w-1.5 h-1.5 rounded-full shrink-0"
        style={{ background: DOT[tone] }}
      />
      <span className="min-w-0 flex-1 flex flex-col gap-0.5">
        <span className="flex items-center gap-x-2 gap-y-1 min-w-0 sm:flex-wrap">
          <span className="text-sm font-medium truncate" style={{ color: C.textPrimary }}>
            {name}
          </span>
          {nameSuffix}
          {/* Chips ride the name line on desktop only. */}
          <span className="hidden sm:contents">
            {chips}
            {meta}
          </span>
        </span>
        {detail && <span className="hidden sm:flex items-center gap-2 flex-wrap">{detail}</span>}
      </span>
      {(action || overflow || showAccessory) && (
        <span className="flex items-center gap-1 sm:gap-1.5 shrink-0">
          {action}
          {overflow}
          {showAccessory && (
            <button
              type="button"
              onClick={() => setExpanded((v) => !v)}
              aria-expanded={expanded}
              aria-label={expanded ? t("collapse") : t("expand")}
              data-testid={testId ? `${testId}-expand` : undefined}
              className="sm:hidden flex items-center justify-center w-11 h-11 min-w-11 -mr-1 rounded-md cursor-pointer"
            >
              <ChevronDown
                size={14}
                aria-hidden
                className="transition-transform duration-150 motion-reduce:transition-none"
                style={{ color: C.textDim, transform: expanded ? "rotate(180deg)" : "none" }}
              />
            </button>
          )}
        </span>
      )}
    </div>
  );

  const classes = cn(
    "rounded-md border px-2.5 py-1.5 w-full text-left transition-colors",
    muted && "opacity-60",
    onClick && "cursor-pointer hover:bg-[var(--color-bg-hover)]",
    className,
  );
  const style = { background: C.bgElevated, borderColor: C.borderSubtle } as const;

  const inner = (
    <>
      {head}

      {/* Mobile: one summary line under the name; the whole cell is the
          disclosure target, the way a native list row behaves. The action
          buttons sit above it in the head and stop propagation themselves. */}
      {summary && (
        <div
          className="sm:hidden truncate text-[11px] leading-4"
          style={{ color: C.textMuted }}
        >
          {summary}
        </div>
      )}

      {expanded && (
        <div className="sm:hidden flex flex-col gap-2 pt-2">
          {(chips || meta) && (
            <div className="flex items-center gap-x-2 gap-y-1.5 flex-wrap">
              {chips}
              {meta}
            </div>
          )}
          {detail && <div className="flex items-center gap-2 flex-wrap">{detail}</div>}
        </div>
      )}
    </>
  );

  return (
    <div
      data-testid={testId}
      {...dataAttrs}
      className={cn(classes, onClick && "cursor-pointer")}
      style={style}
      {...(onClick
        ? {
            role: "button",
            tabIndex: 0,
            onClick,
            onKeyDown: (e: React.KeyboardEvent) => {
              if (e.target !== e.currentTarget) return;
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                onClick();
              }
            },
          }
        : {})}
    >
      {inner}
    </div>
  );
}
