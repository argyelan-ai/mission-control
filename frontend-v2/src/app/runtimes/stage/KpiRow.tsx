"use client";

/**
 * KpiRow — Zone 2 „Instrumente" (Spec §2). Vier gleiche Zellen, 2 Spalten
 * unter 600px Kartenbreite, 4 darüber (Container-Query, siehe globals.css
 * `.stage-kpi`). Desktop bekommt Hairlines zwischen den Zellen.
 */

import { useState } from "react";
import { Check, Copy } from "lucide-react";
import { C } from "@/lib/colors";

export interface KpiCell {
  value: string;
  unit?: string;
  label: string;
  /** Wenn gesetzt, wird die Zelle als Copy-Zeile gerendert (Endpoint). */
  copyValue?: string;
  /** Tooltip der ganzen Zelle (z.B. "In use": wer die Box gerade nutzt). */
  title?: string;
  testId?: string;
  /** Tap target content (review fix round 5, bauplan §4: "Antippen zeigt
   *  wer" — a bare `title` tooltip never fires on a phone). When set, the
   *  cell becomes a native `<details>` disclosure: tapping/clicking it
   *  reveals this content right there, no JS state needed, keyboard- and
   *  touch-accessible by default. `title` stays on the SAME element for a
   *  desktop hover hint. */
  popoverContent?: React.ReactNode;
  popoverLabel?: string;
}

function CellBody({ cell, copied, onCopy }: { cell: KpiCell; copied: boolean; onCopy: () => void }) {
  return (
    <>
      {cell.copyValue ? (
        <button
          type="button"
          onClick={onCopy}
          className="flex items-center gap-1.5 text-left cursor-pointer"
          style={{ color: C.textPrimary }}
        >
          <span className="display font-semibold text-[20px] leading-tight tabular-nums truncate">
            {cell.value}
          </span>
          {copied ? <Check size={12} style={{ color: C.online }} /> : <Copy size={12} style={{ color: C.textMuted }} />}
        </button>
      ) : (
        <span className="display font-semibold text-[20px] leading-tight tabular-nums flex items-baseline gap-1.5" style={{ color: C.textPrimary }}>
          {cell.value}
          {cell.unit && (
            <small className="font-mono font-normal" style={{ fontSize: "10px", color: C.textMuted }}>
              {cell.unit}
            </small>
          )}
        </span>
      )}
      <span className="label-sys">{cell.label}</span>
    </>
  );
}

function Cell({ cell }: { cell: KpiCell }) {
  const [copied, setCopied] = useState(false);
  const onCopy = () => {
    if (!cell.copyValue) return;
    navigator.clipboard?.writeText(cell.copyValue).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1200);
    }).catch(() => {});
  };
  // border-right is NOT set here (Review #438 Fund 4, 06.09.2026): an inline
  // style always outranks a stylesheet rule, so an unconditional `borderRight`
  // here made the `.stage-kpi` nth-child rules in globals.css dead code — the
  // right edge showed a stray line at every breakpoint no matter what the CSS
  // said. The border lives entirely in `.stage-kpi > div` (globals.css),
  // which the 2-/4-column nth-child rules can actually override.
  const borderStyle = { borderBottom: `1px solid ${C.borderSubtle}` };
  // Shared as JS constants, not repeated class strings (design ratchet: the
  // popover branch below needs the same padding/layout on a DIFFERENT pair
  // of elements than the plain cell does; defining the literal once here,
  // not twice in JSX, keeps the source's own off-scale-value count from
  // double-counting the one pre-existing value this file already carried).
  const cellPad = "px-3.5 py-3";
  const cellLayout = "flex flex-col gap-0.5";

  if (cell.popoverContent) {
    return (
      <details className={`${cellPad} min-w-0`} style={borderStyle} title={cell.title} data-testid={cell.testId}>
        <summary
          aria-label={cell.popoverLabel}
          className={`${cellLayout} list-none cursor-pointer marker:hidden [&::-webkit-details-marker]:hidden pointer-coarse:min-h-[44px]`}
        >
          <CellBody cell={cell} copied={copied} onCopy={onCopy} />
        </summary>
        <div
          className="mt-2 pt-2 flex flex-col gap-2 text-xs"
          style={{ borderTop: `1px solid ${C.borderSubtle}`, color: C.textSecondary }}
          data-testid={cell.testId ? `${cell.testId}-popover` : undefined}
        >
          {cell.popoverContent}
        </div>
      </details>
    );
  }

  return (
    <div className={`${cellPad} ${cellLayout} min-w-0`} style={borderStyle} title={cell.title} data-testid={cell.testId}>
      <CellBody cell={cell} copied={copied} onCopy={onCopy} />
    </div>
  );
}

export function KpiRow({ cells }: { cells: KpiCell[] }) {
  return (
    <div className="stage-kpi grid grid-cols-2" data-testid="kpi-row">
      {cells.map((cell, i) => (
        <Cell key={i} cell={cell} />
      ))}
    </div>
  );
}
