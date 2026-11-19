"use client";

import { cn } from "@/lib/utils";

import { alpha } from "@/lib/colors";
interface PillProps extends Pick<React.HTMLAttributes<HTMLSpanElement>, "title" | "aria-label" | "role"> {
  children: React.ReactNode;
  "data-testid"?: string;
  color: string;
  variant?: "solid" | "outline";
  size?: "sm" | "md";
  /** "sentence": the word as written, without the uppercase marker style —
   *  for state words in a page body (DESIGN K7/K12: uppercase marks are
   *  rare). Default "upper" keeps every existing pill unchanged. */
  casing?: "upper" | "sentence";
  className?: string;
}

export function Pill({
  children,
  color,
  variant = "solid",
  size = "sm",
  casing = "upper",
  className,
  ...rest
}: PillProps) {
  const isSolid = variant === "solid";

  return (
    <span
      {...rest}
      className={cn(
        "inline-flex items-center rounded-sm leading-none whitespace-nowrap",
        casing === "upper" && "font-semibold uppercase tracking-[0.06em]",
        casing === "sentence" && "font-medium",
        casing === "upper" && size === "sm" && "px-2 py-1 text-[10px]",
        casing === "sentence" && size === "sm" && "gap-1 px-2 py-1 text-xs",
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
