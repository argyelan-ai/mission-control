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
  return (
    <div
      className="px-3.5 py-3 flex flex-col gap-0.5 min-w-0"
      // border-right is NOT set here (Review #438 Fund 4, 06.09.2026): an
      // inline style always outranks a stylesheet rule, so the previous
      // unconditional `borderRight` here made the `.stage-kpi` nth-child
      // rules in globals.css dead code — the right edge showed a stray line
      // at every breakpoint no matter what the CSS said. The border now
      // lives entirely in `.stage-kpi > div` (globals.css), which the
      // 2-/4-column nth-child rules can actually override.
      style={{ borderBottom: `1px solid ${C.borderSubtle}` }}
    >
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
