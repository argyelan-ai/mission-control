"use client";

import { motion, AnimatePresence, useReducedMotion } from "framer-motion";
import { cn } from "@/lib/utils";
import { useBodyScrollLock } from "@/hooks/useBodyScrollLock";

interface ResponsiveModalProps {
  open: boolean;
  onClose: () => void;
  children: React.ReactNode;
  className?: string;
  "aria-label"?: string;
  "aria-labelledby"?: string;
}

/**
 * Mobile-safe modal wrapper.
 * - Mobile (< sm): full-width with 8px side margins, slides up from bottom
 * - Desktop (≥ sm): centered, max-w-[680px], scales in
 */
export function ResponsiveModal({
  open,
  onClose,
  children,
  className,
  "aria-label": ariaLabel,
  "aria-labelledby": ariaLabelledBy,
}: ResponsiveModalProps) {
  const prefersReducedMotion = useReducedMotion();

  // iOS-safe scroll lock (M4) — fixed-position technique instead of overflow:hidden
  useBodyScrollLock(open);

  return (
    <AnimatePresence>
      {open && (
        <motion.div
          initial={prefersReducedMotion ? false : { opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: prefersReducedMotion ? 0 : 0.15 }}
          className="fixed inset-0 z-50 flex items-end sm:items-center justify-center sm:p-4"
          style={{ paddingBottom: "env(safe-area-inset-bottom)" }}
          onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}
        >
          {/* Backdrop */}
          <div
            className="absolute inset-0"
            style={{ backgroundColor: "rgba(0,0,0,0.65)" }}
          />

          {/* Panel */}
          <motion.div
            role="dialog"
            aria-modal="true"
            aria-label={ariaLabel}
            aria-labelledby={ariaLabelledBy}
            initial={prefersReducedMotion ? false : { opacity: 0, y: 24 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: 24 }}
            transition={{ duration: prefersReducedMotion ? 0 : 0.22, ease: [0.16, 1, 0.3, 1] }}
            className={cn(
              "relative w-full flex flex-col overflow-hidden",
              // Mobile: full-width with margins, rounded top, max 92vh
              "mx-2 rounded-t-2xl rounded-b-none max-h-[92dvh]",
              // Desktop: centered, max-width, fully rounded
              "sm:mx-0 sm:max-w-[680px] sm:rounded-2xl sm:max-h-[88vh]",
              className
            )}
            style={{
              backgroundColor: "var(--color-bg-elevated)",
              border: "1px solid rgba(255,255,255,0.08)",
              boxShadow: "0 4px 24px rgba(0,0,0,0.5), 0 1px 2px rgba(0,0,0,0.3)",
            }}
          >
            {/* Top drag indicator on mobile */}
            <div className="sm:hidden flex justify-center pt-2.5 pb-0 shrink-0">
              <div className="w-8 h-1 rounded-full" style={{ backgroundColor: "rgba(255,255,255,0.12)" }} />
            </div>

            {children}
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
