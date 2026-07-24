"use client";

import type { LucideIcon } from "lucide-react";
import { TrendingUp, TrendingDown, Minus } from "lucide-react";
import { cn } from "@/lib/utils";

interface KPICardProps {
  label: string;
  value: string | number;
  icon?: LucideIcon;
  trend?: "up" | "down" | "neutral";
  trendValue?: string;
  className?: string;
}

const trendConfig = {
  up: {
    Icon: TrendingUp,
    color: "var(--color-status-online)",
  },
  down: {
    Icon: TrendingDown,
    color: "var(--color-status-error)",
  },
  neutral: {
    Icon: Minus,
    color: "var(--color-text-muted)",
  },
} as const;

export function KPICard({
  label,
  value,
  icon: Icon,
  trend,
  trendValue,
  className,
}: KPICardProps) {
  const trendInfo = trend ? trendConfig[trend] : null;
  // Long string values ("tomorrow 07:00") blow out half the card on mobile —
  // numbers stay large, text scales down on mobile (desktop unchanged).
  const isLongText = typeof value === "string" && value.length > 6;

  return (
    <div
      className={cn("rounded-md p-5 max-sm:p-4", className)}
      style={{
        background: "var(--color-bg-surface)",
        border: "1px solid var(--color-border)",
      }}
    >
      <div className="flex items-start justify-between gap-3">
        <span className="label-sys">{label}</span>
        {Icon && (
          <Icon
            size={15}
            className="text-[var(--color-text-muted)] shrink-0"
          />
        )}
      </div>

      <div className="mt-3 flex items-end gap-3">
        <span
          className={cn(
            "display font-semibold text-[var(--color-text-primary)] min-w-0 break-words",
            isLongText
              ? "text-[30px] max-sm:text-lg max-sm:leading-snug"
              : "text-[30px]"
          )}
        >
          {value}
        </span>

        {trendInfo && trendValue && (
          <span
            className="mb-1 flex items-center gap-1 font-mono text-[10px]"
            style={{ color: trendInfo.color }}
          >
            <trendInfo.Icon size={12} />
            {trendValue}
          </span>
        )}
      </div>
    </div>
  );
}
