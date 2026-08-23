"use client";

import { cn } from "@/lib/utils";
import { C, STATUS } from "@/lib/colors";

type Status = "online" | "warning" | "error" | "busy" | "idle" | "offline";
type Size = "sm" | "md" | "lg";

interface StatusDotProps {
  status: Status;
  size?: Size;
  pulse?: boolean;
  className?: string;
}

const statusColors: Record<Status, string> = {
  online: C.online,    // #55A964
  warning: C.warning,  // #A67F3E
  error: C.error,      // #FA4942
  busy: STATUS.busy,   // #5890CA — busy is an info state, not the accent
  idle: C.textDim,     // #8A8A8A
  offline: STATUS.offline, // #3A3A3A
};

const sizeMap: Record<Size, number> = {
  sm: 6,
  md: 8,
  lg: 10,
};

export function StatusDot({
  status,
  size = "md",
  pulse = false,
  className,
}: StatusDotProps) {
  const color = statusColors[status];
  const px = sizeMap[size];
  const shouldPulse = pulse && status !== "offline" && status !== "idle";

  return (
    <span
      className={cn("relative inline-flex shrink-0", className)}
      style={{ width: px, height: px }}
    >
      <span
        className="absolute inset-0 rounded-full"
        style={{ backgroundColor: color }}
      />
      {shouldPulse && (
        <span
          className="absolute inset-0 rounded-full animate-status-pulse"
          style={
            {
              "--pulse-color": color,
            } as React.CSSProperties
          }
        />
      )}

      {/* Inline keyframes via style tag for the pulse animation */}
      {shouldPulse && (
        <style>{`
          @keyframes status-pulse {
            0%, 100% {
              box-shadow: 0 0 0 0 var(--pulse-color, ${color});
              opacity: 0.6;
            }
            50% {
              box-shadow: 0 0 0 4px var(--pulse-color, ${color});
              opacity: 0;
            }
          }
          .animate-status-pulse {
            animation: status-pulse 2s ease-in-out infinite;
          }
        `}</style>
      )}
    </span>
  );
}
