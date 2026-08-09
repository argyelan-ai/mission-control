"use client";

/**
 * HostSection — generic section wrapper used by OverviewTab for host groups,
 * Cloud, and Unassigned. Header row (title + optional metrics bar/subtitle) +
 * divider + children. `dimmed` wraps children in opacity-60 (host is
 * power-managed and none of its runtimes are ready — cheap host-side signal,
 * no extra query).
 */

import type { ReactNode } from "react";
import { C } from "@/lib/colors";
import type { Host } from "@/lib/types";
import { SingleHostMetricsBar } from "./HostsSection";

export function HostSection({
  title,
  subtitle,
  metricsHost,
  dimmed,
  children,
}: {
  title: string;
  subtitle?: string;
  metricsHost?: Host;
  dimmed?: boolean;
  children: ReactNode;
}) {
  return (
    <div className="mb-6">
      <div className="flex flex-col gap-2 mb-3">
        <div className="flex items-center gap-2">
          <h2 className="text-sm font-semibold" style={{ color: C.textPrimary }}>
            {title}
          </h2>
          {subtitle && (
            <>
              <span style={{ color: C.borderSubtle }}>·</span>
              <span className="text-xs" style={{ color: C.textMuted }}>
                {subtitle}
              </span>
            </>
          )}
        </div>
        {metricsHost && <SingleHostMetricsBar host={metricsHost} />}
      </div>
      <div className="h-px mb-3" style={{ background: C.borderSubtle }} />
      <div className={dimmed ? "opacity-60" : undefined}>{children}</div>
    </div>
  );
}
