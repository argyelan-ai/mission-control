"use client";

import { forwardRef } from "react";
import { cn } from "@/lib/utils";

interface GlassCardProps extends React.HTMLAttributes<HTMLDivElement> {
  glow?: string;
}

export const GlassCard = forwardRef<HTMLDivElement, GlassCardProps>(
  ({ children, className, glow, onClick, ...props }, ref) => {
    return (
      <div
        ref={ref}
        onClick={onClick}
        className={cn(
          "relative rounded-xl",
          "bg-[rgba(255,255,255,0.03)]",
          "backdrop-blur-[16px]",
          "border border-[rgba(255,255,255,0.07)]",
          "transition-shadow duration-200",
          onClick && "cursor-pointer",
          className
        )}
        style={
          glow
            ? { boxShadow: `0 0 24px 0 ${glow}` }
            : undefined
        }
        {...props}
      >
        {/* Top-edge highlight */}
        <div
          aria-hidden
          className="pointer-events-none absolute inset-x-0 top-0 h-px rounded-t-xl"
          style={{
            background:
              "linear-gradient(90deg, transparent, rgba(255,255,255,0.12) 50%, transparent)",
          }}
        />
        {children}
      </div>
    );
  }
);

GlassCard.displayName = "GlassCard";
