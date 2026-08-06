"use client";

import { C, STATUS_TEXT } from "@/lib/colors";
import { cn } from "@/lib/utils";

/**
 * The one list grammar for /runtimes.
 *
 * The page had grown eight row components (runtimes, LM Studio models, hosts,
 * CLI tools, catalog providers, catalog models, local recipes, discovered
 * containers) and roughly a dozen chip styles across three radii, three text
 * sizes and three padding scales. Every section had invented its own row.
 *
 * Every row on the page is now this component, in this order:
 *
 *   [status dot] name [meta chips] ............ [one primary action] [overflow]
 *
 * and every chip is a MetaChip. If a row needs something this cannot express,
 * the answer is to extend this component, not to hand-roll a row next to it.
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
  children,
}: {
  tone?: Tone;
  icon?: React.ReactNode;
  title?: string;
  className?: string;
  testId?: string;
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
      style={{ color: c.color, border: `1px solid ${c.border}`, background: c.background }}
    >
      {icon}
      {children}
    </span>
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
      className={cn(
        "min-w-0 truncate text-[11px]",
        mono && "font-mono",
        className,
      )}
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
  const body = (
    <>
      {leading}
      <span
        aria-hidden
        className="w-1.5 h-1.5 rounded-full shrink-0"
        style={{ background: DOT[tone] }}
      />
      <span className="min-w-0 flex-1 flex flex-col gap-0.5">
        <span className="flex items-center gap-x-2 gap-y-1 flex-wrap min-w-0">
          <span
            className="text-sm font-medium truncate"
            style={{ color: C.textPrimary }}
          >
            {name}
          </span>
          {nameSuffix}
          {chips}
          {meta}
        </span>
        {detail && <span className="flex items-center gap-2 flex-wrap">{detail}</span>}
      </span>
      {(action || overflow) && (
        <span className="flex items-center gap-1.5 shrink-0">
          {action}
          {overflow}
        </span>
      )}
    </>
  );

  const classes = cn(
    "flex items-center gap-2 rounded-md border px-2.5 py-1.5 min-h-[38px] w-full text-left transition-colors",
    muted && "opacity-60",
    onClick && "cursor-pointer hover:bg-[var(--color-bg-hover)]",
    className,
  );
  const style = {
    background: C.bgElevated,
    borderColor: C.borderSubtle,
  } as const;

  if (onClick) {
    return (
      <button type="button" onClick={onClick} data-testid={testId} {...dataAttrs} className={classes} style={style}>
        {body}
      </button>
    );
  }
  return (
    <div data-testid={testId} {...dataAttrs} className={classes} style={style}>
      {body}
    </div>
  );
}
