"use client";

import Link from "next/link";
import { ChevronRight, type LucideIcon } from "lucide-react";
import { C } from "@/lib/colors";

export type LinkListItem = {
  href: string;
  icon: LucideIcon;
  title: string;
  hint?: string;
  /** A quiet count at the right edge ("212 notes"); absent while unknown. */
  count?: string | null;
};

/**
 * A calm list of destinations: icon, title, an optional one-line hint, a
 * chevron. One left edge, rows separated by hairlines, no cards (K8, K9) —
 * the Knowledge landing and Settings › More both use it.
 */
export function LinkList({ items, testId }: { items: LinkListItem[]; testId?: string }) {
  return (
    <ul data-testid={testId} className="flex flex-col">
      {items.map((item, i) => {
        const Icon = item.icon;
        return (
          <li key={item.href} style={i > 0 ? { borderTop: `1px solid ${C.borderSubtle}` } : undefined}>
            <Link
              href={item.href}
              className="flex items-center gap-3 min-h-14 py-2 -mx-2 px-2 rounded-lg transition-colors hover:bg-[var(--color-bg-hover)]"
            >
              <Icon size={20} strokeWidth={1.75} aria-hidden style={{ color: C.textMuted }} className="shrink-0" />
              <span className="flex-1 min-w-0 flex flex-col">
                <span className="text-base" style={{ color: C.textPrimary, fontWeight: 500 }}>
                  {item.title}
                </span>
                {item.hint && (
                  <span className="text-sm truncate" style={{ color: C.textMuted }}>
                    {item.hint}
                  </span>
                )}
              </span>
              {item.count && (
                <span data-testid="link-list-count" className="shrink-0 text-sm tabular-nums" style={{ color: C.textMuted }}>
                  {item.count}
                </span>
              )}
              <ChevronRight size={18} aria-hidden style={{ color: C.textMuted }} className="shrink-0" />
            </Link>
          </li>
        );
      })}
    </ul>
  );
}
