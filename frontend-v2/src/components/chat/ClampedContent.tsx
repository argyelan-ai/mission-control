"use client";

import { useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { C } from "@/lib/colors";

// ── User-bubble clamp ───────────────────────────────────────────────────────
// A dispatch brief is thousands of characters. Left unclamped it becomes a wall
// the reader has to scroll past every single time to reach what the agent
// actually did. Ten lines is enough to recognise which brief this is; the rest
// is one tap away.
const USER_CLAMP_LINES = 10;
const USER_LINE_HEIGHT_PX = 23; // 14px × 1.6, rounded — matches the compact `p`
export const USER_CLAMP_MAX_PX = USER_CLAMP_LINES * USER_LINE_HEIGHT_PX;

/** A body that is clamped until the reader asks for the rest. The expander
 *  only appears when there is genuinely something hidden. Shared by the
 *  operator bubble (markdown) and the teammate row (plain text): a dispatch
 *  file mention from omp arrives as a teammate turn and carried the whole
 *  Operating Card — 300 lines in the middle of the history (04.09.2026). */
export function ClampedContent({
  text,
  testId,
  className,
  style,
  children,
}: {
  text: string;
  testId: string;
  className?: string;
  style?: React.CSSProperties;
  children: React.ReactNode;
}) {
  // Der Aufklapper stand bis 22.08.2026 fest auf Deutsch — in der
  // englischen Oberflaeche also mitten im Satz die falsche Sprache.
  const tChat = useTranslations("chat");
  const [expanded, setExpanded] = useState(false);
  const [overflows, setOverflows] = useState(false);
  const contentRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = contentRef.current;
    if (!el) return;
    const measure = () => {
      // Same trap as the composer's auto-grow: the mobile stack keeps the
      // off-screen pane mounted with `display: none`, where every metric reads
      // 0 and would report "nothing is hidden" for a 3000-character brief.
      if (el.scrollHeight === 0) return;
      setOverflows(el.scrollHeight > USER_CLAMP_MAX_PX + USER_LINE_HEIGHT_PX / 2);
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, [text]);

  const clamped = overflows && !expanded;

  return (
    <>
      <div
        ref={contentRef}
        data-testid={testId}
        data-clamped={clamped}
        className={className}
        style={{ ...style, ...(clamped ? { maxHeight: USER_CLAMP_MAX_PX, overflow: "hidden" } : {}) }}
      >
        {children}
      </div>
      {overflows && (
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          aria-expanded={expanded}
          className="mt-1.5 pt-1.5 w-full text-left text-[12px] font-medium cursor-pointer transition-colors"
          style={{ color: C.textMuted, borderTop: `1px solid ${C.borderSubtle}` }}
        >
          {expanded ? tChat("showLess") : tChat("showMore")}
        </button>
      )}
    </>
  );
}

