"use client";

import { forwardRef } from "react";
import { cn } from "@/lib/utils";

interface GlassCardProps extends React.HTMLAttributes<HTMLDivElement> {
  glow?: string;
}

/**
 * v3: historischer Name — ist längst keine Glass-Card mehr.
 * Flache Surface: bg-surface, 1px Border, eckige Radien, kein Blur,
 * kein Highlight-Streak, kein Glow-Schatten. `glow` wird akzeptiert
 * (API-Kompatibilität), aber ignoriert — farbige Schatten sind v3-verboten.
 */
export const GlassCard = forwardRef<HTMLDivElement, GlassCardProps>(
  ({ children, className, glow: _glow, onClick, ...props }, ref) => {
    return (
      <div
        ref={ref}
        onClick={onClick}
        className={cn(
          "relative rounded-md",
          "bg-[var(--color-bg-surface)]",
          "border border-[var(--color-border)]",
          "transition-colors duration-200",
          onClick && "cursor-pointer",
          className
        )}
        {...props}
      >
        {children}
      </div>
    );
  }
);

GlassCard.displayName = "GlassCard";
