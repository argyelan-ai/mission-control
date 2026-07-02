"use client";

import { X } from "lucide-react";
import type { ReactNode } from "react";

/**
 * CardShell — shared wrapper that gives all Voice-Display cards the
 * same glass strip aesthetic. Each card type fills the children slot
 * with its specific layout.
 *
 * Visual rules:
 * - Thin 1px teal stripe on the left edge identifies "voice-pushed"
 *   content vs. native MC UI. (was purple rgba(168,85,247) — de-purpled)
 * - Same translucent surface as the parent VoiceDrawer so it doesn't
 *   look bolted on.
 * - Top-right `X` removes only this card (parent owns the state).
 */
export function CardShell({
  children,
  icon,
  kind,
  meta,
  onClose,
}: {
  children: ReactNode;
  icon: ReactNode;
  kind: string;
  meta?: string | null;
  onClose: () => void;
}) {
  return (
    <div
      className="relative flex items-start gap-2 px-2.5 py-2 rounded-lg overflow-hidden"
      style={{
        background: "rgba(255,255,255,0.025)",
        border: "1px solid rgba(255,255,255,0.05)",
      }}
    >
      {/* left edge marker */}
      <div
        className="absolute left-0 top-2 bottom-2 w-px rounded-full"
        style={{
          background: "linear-gradient(to bottom, rgba(15,163,163,0.7), rgba(15,163,163,0.15))",
        }}
      />

      {/* icon + kind label column */}
      <div className="flex flex-col items-center pt-0.5 shrink-0 w-5">
        {icon}
        {meta && (
          <span
            className="text-[8px] uppercase tracking-wider mt-1 truncate max-w-[40px]"
            style={{ color: "var(--color-text-muted)", letterSpacing: "0.08em" }}
            title={meta}
          >
            {kind}
          </span>
        )}
      </div>

      {/* content */}
      <div className="min-w-0 flex-1">{children}</div>

      {/* close */}
      <button
        type="button"
        onClick={onClose}
        className="shrink-0 p-1 rounded hover:bg-white/5 transition-colors cursor-pointer -mr-1 -mt-1"
        aria-label="Karte schliessen"
      >
        <X size={10} style={{ color: "var(--color-text-muted)" }} />
      </button>
    </div>
  );
}
