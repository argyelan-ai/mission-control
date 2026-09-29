"use client";

import { cn } from "@/lib/utils";

import { alpha } from "@/lib/colors";
interface PillProps {
  children: React.ReactNode;
  color: string;
  variant?: "solid" | "outline";
  size?: "sm" | "md";
  className?: string;
}

export function Pill({
  children,
  color,
  variant = "solid",
  size = "sm",
  className,
}: PillProps) {
  const isSolid = variant === "solid";

  return (
    <span
      className={cn(
        "inline-flex items-center rounded-sm font-semibold uppercase tracking-[0.06em] leading-none whitespace-nowrap",
        size === "sm" && "px-2 py-1 text-[10px]",
        size === "md" && "px-2.5 py-1.5 text-[11px]",
        className
      )}
      style={{
        color,
        backgroundColor: isSolid ? alpha(color, 0.12) : "transparent",
        border: `1px solid ${alpha(color, 0.15)}`,
      }}
    >
      {children}
    </span>
  );
}
