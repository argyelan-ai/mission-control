"use client";

import { Children, useState } from "react";
import { useTranslations } from "next-intl";
import { ChevronDown, ChevronUp } from "lucide-react";
import { C } from "@/lib/colors";
import { cn } from "@/lib/utils";

/**
 * A list that shows a fixed number of rows and hides the rest behind a control.
 *
 * Two earlier attempts capped by pixel height. `max-height: 22rem` sliced
 * whichever row straddled the line, which reads as a rendering bug. Measuring
 * the rows and capping at the nearest row boundary fixed the slicing but raced
 * with late layout: a clipped container does not report content shrinkage to a
 * ResizeObserver, so the state could keep a stale "show 1 more" for rows that
 * were already fully on screen.
 *
 * Capping by row count has neither problem. The boundary is exact by
 * construction, the hidden count is exact arithmetic, and there is no
 * measurement to go stale. Hidden rows are not rendered at all, which also
 * keeps long provider lists out of the DOM until they are asked for.
 */
export function CappedList({
  /** Rows visible while collapsed. */
  maxRows = 6,
  className,
  children,
  testId,
}: {
  maxRows?: number;
  className?: string;
  children: React.ReactNode;
  testId?: string;
}) {
  const t = useTranslations("common.cappedList");
  const [expanded, setExpanded] = useState(false);

  const rows = Children.toArray(children);
  const hidden = Math.max(0, rows.length - maxRows);
  const capped = hidden > 0 && !expanded;
  const shown = capped ? rows.slice(0, maxRows) : rows;

  return (
    <div data-testid={testId}>
      <div className="relative">
        <div className={cn("flex flex-col gap-1.5", className)}>{shown}</div>
        {capped && (
          // Soft edge under the last full row: signals "the list continues"
          // without dimming any row, because it sits in the gap below them.
          <div
            aria-hidden
            className="pointer-events-none absolute inset-x-0 -bottom-4 h-8"
            style={{
              background:
                "linear-gradient(to bottom, transparent, var(--color-p2-bg) 70%)",
            }}
          />
        )}
      </div>

      {(capped || expanded) && (
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          data-testid={testId ? `${testId}-toggle` : undefined}
          className="mt-4 inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs cursor-pointer transition-colors hover:bg-[var(--color-bg-surface)]"
          style={{ color: C.textSecondary }}
        >
          {expanded ? (
            <>
              <ChevronUp size={12} />
              {t("showFewer")}
            </>
          ) : (
            <>
              <ChevronDown size={12} />
              {t("showAll", { n: hidden })}
            </>
          )}
        </button>
      )}
    </div>
  );
}
