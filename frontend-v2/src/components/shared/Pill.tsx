"use client";

import { cn } from "@/lib/utils";

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
        "inline-flex items-center rounded-full font-semibold uppercase tracking-[0.06em] leading-none whitespace-nowrap",
        size === "sm" && "px-2 py-1 text-[10px]",
        size === "md" && "px-2.5 py-1.5 text-[11px]",
        className
      )}
      style={{
        color,
        backgroundColor: isSolid ? `${color}1F` : "transparent",
        border: `1px solid ${color}26`,
        textShadow: isSolid ? `0 0 12px ${color}40` : "none",
      }}
    >
      {children}
    </span>
  );
}
