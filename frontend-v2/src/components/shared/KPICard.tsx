"use client";

import type { LucideIcon } from "lucide-react";
import { TrendingUp, TrendingDown, Minus } from "lucide-react";
import { cn } from "@/lib/utils";
import { GlassCard } from "./GlassCard";

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
  // Lange String-Werte ("morgen 07:00") sprengen mobil die halbe Karte —
  // Zahlen bleiben 3xl, Text skaliert auf Mobile runter (Desktop unverändert).
  const isLongText = typeof value === "string" && value.length > 6;

  return (
    <GlassCard className={cn("p-5 max-sm:p-4", className)}>
      <div className="flex items-start justify-between gap-3">
        <span className="text-[13px] font-medium text-[var(--color-text-secondary)] tracking-wide">
          {label}
        </span>
        {Icon && (
          <Icon
            size={16}
            className="text-[var(--color-text-muted)] shrink-0"
          />
        )}
      </div>

      <div className="mt-3 flex items-end gap-3">
        <span
          className={cn(
            "font-semibold tracking-tight text-[var(--color-text-primary)] min-w-0 break-words",
            isLongText ? "text-3xl max-sm:text-lg max-sm:leading-snug" : "text-3xl"
          )}
        >
          {value}
        </span>

        {trendInfo && trendValue && (
          <span
            className="mb-1 flex items-center gap-1 text-xs font-medium"
            style={{ color: trendInfo.color }}
          >
            <trendInfo.Icon size={13} />
            {trendValue}
          </span>
        )}
      </div>
    </GlassCard>
  );
}
