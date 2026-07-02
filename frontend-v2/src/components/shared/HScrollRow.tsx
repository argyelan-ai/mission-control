import { cn } from "@/lib/utils";

interface HScrollRowProps {
  children: React.ReactNode;
  className?: string;
}

/**
 * Horizontal scrolling strip for card collections on mobile.
 * - Mobile: horizontal scroll with snap, negative margins to bleed to screen edge
 * - Desktop (≥ md): normal flex-wrap (no scroll)
 */
export function HScrollRow({ children, className }: HScrollRowProps) {
  return (
    <div
      className={cn(
        // Mobile: bleed to screen edge, scroll-snap
        "flex gap-3 -mx-4 px-4 overflow-x-auto scroll-x-snap",
        // Desktop: wrap normally, no overflow
        "md:mx-0 md:px-0 md:overflow-visible md:flex-wrap",
        className
      )}
    >
      {children}
    </div>
  );
}
