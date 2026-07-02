"use client";

import { useEffect } from "react";
import { motion, AnimatePresence, useReducedMotion } from "framer-motion";
import { X } from "lucide-react";
import { cn } from "@/lib/utils";

interface SlideOverPanelProps {
  open: boolean;
  onClose: () => void;
  children: React.ReactNode;
  className?: string;
  title?: string;
  /** Width on md+ screens. Default: 420px */
  desktopWidth?: string;
}

/**
 * Slide-over detail panel:
 * - Mobile: full-screen overlay (slides in from bottom)
 * - Desktop (≥ md): fixed-width side panel (slides in from right)
 */
export function SlideOverPanel({
  open,
  onClose,
  children,
  className,
  title,
  desktopWidth = "420px",
}: SlideOverPanelProps) {
  const prefersReducedMotion = useReducedMotion();

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    if (open) window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  return (
    <AnimatePresence>
      {open && (
        <>
          {/* Backdrop — only on mobile (md+ the panel sits beside content) */}
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: prefersReducedMotion ? 0 : 0.2 }}
            className="fixed inset-0 z-40 md:hidden"
            style={{ backgroundColor: "rgba(0,0,0,0.6)" }}
            onClick={onClose}
          />

          {/* Panel */}
          <motion.div
            role="dialog"
            aria-modal="true"
            aria-label={title}
            initial={prefersReducedMotion ? false : { opacity: 0, y: "100%" }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: "100%" }}
            // On md+: slide from right instead
            className={cn(
              "fixed z-50 flex flex-col overflow-hidden",
              // Mobile: full-screen from bottom
              "inset-x-0 bottom-0 rounded-t-2xl max-h-[94dvh]",
              // Desktop: right side panel, full height
              "md:inset-x-auto md:right-0 md:top-0 md:bottom-0 md:max-h-full md:rounded-none md:rounded-l-2xl",
              className
            )}
            style={{
              width: "100%",
              "--panel-w": desktopWidth,
              backgroundColor: "var(--color-bg-elevated)",
              borderLeft: "1px solid var(--color-border-subtle)",
              boxShadow: "-8px 0 40px rgba(0,0,0,0.4)",
            } as React.CSSProperties}
            // Set desktop width via inline style targeting
            onAnimationStart={() => {}}
          >
            {/* Mobile drag handle */}
            <div className="md:hidden flex justify-center pt-2.5 shrink-0">
              <div className="w-8 h-1 rounded-full" style={{ backgroundColor: "rgba(255,255,255,0.12)" }} />
            </div>

            {/* Header with close button */}
            {title && (
              <div
                className="flex items-center justify-between px-4 py-3 shrink-0"
                style={{ borderBottom: "1px solid var(--color-border-subtle)" }}
              >
                <span className="text-sm font-semibold" style={{ color: "var(--color-text-primary)" }}>
                  {title}
                </span>
                <button
                  onClick={onClose}
                  className="flex items-center justify-center min-h-touch min-w-touch rounded-lg transition-opacity hover:opacity-70 cursor-pointer"
                  style={{ color: "var(--color-text-muted)" }}
                  aria-label="Schließen"
                >
                  <X size={18} />
                </button>
              </div>
            )}

            <div className="flex-1 overflow-y-auto pb-safe">
              {children}
            </div>
          </motion.div>
        </>
      )}
    </AnimatePresence>
  );
}
