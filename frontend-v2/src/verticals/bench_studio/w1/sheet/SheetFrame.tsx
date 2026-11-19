"use client";

// The test sheet's surface (P3): a bottom sheet on the phone (thumb zone,
// grabber, rounded top), a floating side panel over the list from md up
// (mockup 02, 452 px). One overlay, so a 2 px accent edge on top (DESIGN.md
// "Akzent-Kante"), the elevated shadow, a scrim, Esc and the scroll lock.
// The content brings SheetHeader / SheetBody / SheetFooter: the footer
// carries the one Start button, always in reach of the thumb.
import { useEffect, useRef, useSyncExternalStore, type ReactNode } from "react";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { useBodyScrollLock } from "@/hooks/useBodyScrollLock";
import { useEscapeKey } from "@/hooks/useEscapeKey";
import { C, alpha } from "@/lib/colors";

const DESKTOP_QUERY = "(min-width: 768px)";

function subscribe(cb: () => void): () => void {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") return () => {};
  const mq = window.matchMedia(DESKTOP_QUERY);
  mq.addEventListener("change", cb);
  return () => mq.removeEventListener("change", cb);
}

/** md+ (the side panel). False on the server, in jsdom and on the phone. */
export function useDesktop(): boolean {
  return useSyncExternalStore(
    subscribe,
    () => typeof window !== "undefined" && typeof window.matchMedia === "function" && window.matchMedia(DESKTOP_QUERY).matches,
    () => false,
  );
}

interface SheetFrameProps {
  open: boolean;
  onClose: () => void;
  /** Accessible name of the dialog. */
  label: string;
  /** SheetHeader + SheetBody + SheetFooter. */
  children: ReactNode;
}

/** Title row: a fixed slot above the scrolling body. */
export function SheetHeader({ children }: { children: ReactNode }) {
  return <div className="shrink-0 px-4 md:px-6 pt-2 md:pt-4">{children}</div>;
}

/** The scrolling middle. */
export function SheetBody({ children }: { children: ReactNode }) {
  return (
    <div className="flex-1 min-h-0 overflow-y-auto overscroll-contain px-4 md:px-6 pb-4 scroll-quiet">
      {children}
    </div>
  );
}

/** The sticky bottom: summary + the one Start button, in the thumb zone. */
export function SheetFooter({ children }: { children: ReactNode }) {
  return (
    <div className="shrink-0 px-4 md:px-6 pt-3 pb-safe md:pb-6" style={{ borderTop: `1px solid ${C.borderSubtle}` }}>
      {children}
    </div>
  );
}

export function SheetFrame({ open, onClose, label, children }: SheetFrameProps) {
  const reduce = useReducedMotion();
  const desktop = useDesktop();
  const panelRef = useRef<HTMLDivElement>(null);

  useBodyScrollLock(open);
  useEscapeKey(onClose, open);

  // Focus moves into the dialog when it opens (keyboard and screen reader
  // land in the sheet, not behind the scrim).
  useEffect(() => {
    if (open) panelRef.current?.focus({ preventScroll: true });
  }, [open]);

  const from = desktop ? { x: 32, opacity: 0 } : { y: "100%" };
  const to = desktop ? { x: 0, opacity: 1 } : { y: 0 };

  return (
    <AnimatePresence>
      {open && (
        <>
          <motion.div
            key="bench-sheet-scrim"
            aria-hidden
            data-testid="bench-sheet-scrim"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: reduce ? 0 : 0.2 }}
            className="fixed inset-0 z-40"
            style={{ background: alpha(C.scrim, desktop ? 0.45 : 0.7) }}
            onClick={onClose}
          />
          <motion.div
            key="bench-sheet"
            ref={panelRef}
            role="dialog"
            aria-modal="true"
            aria-label={label}
            tabIndex={-1}
            data-region="bench-test-sheet"
            initial={reduce ? { opacity: 0 } : from}
            animate={reduce ? { opacity: 1 } : to}
            exit={reduce ? { opacity: 0 } : from}
            transition={{ duration: reduce ? 0 : 0.28, ease: [0.16, 1, 0.3, 1] }}
            className={
              "fixed z-50 flex flex-col overflow-hidden outline-none " +
              "inset-x-0 bottom-0 max-h-[calc(100dvh-1rem)] rounded-t-2xl " +
              "md:inset-x-auto md:right-3 md:top-3 md:bottom-3 md:max-h-none md:w-[452px] md:rounded-2xl"
            }
            style={{
              background: C.bgElevated,
              borderTop: `2px solid ${C.accent}`,
              boxShadow: "var(--shadow-elevated)",
            }}
          >
            <div className="md:hidden flex justify-center pt-2 shrink-0" aria-hidden>
              <span className="w-10 h-1 rounded-full" style={{ background: C.borderActive }} />
            </div>
            {children}
          </motion.div>
        </>
      )}
    </AnimatePresence>
  );
}
