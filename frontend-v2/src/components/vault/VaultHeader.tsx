"use client";

/**
 * VaultHeader — editorial masthead for the memory page.
 * title: `memory_` in Geist Sans Bold with blinking underscore cursor.
 * subtitle: stats line in uppercase Geist Mono.
 */

import type { ReactNode } from "react";
import { C } from "@/lib/colors";

interface VaultHeaderProps {
  noteCount: number;
  agentCount: number;
  /** Optional action slot rendered on the right side of the header.
   *  Used by VaultMemoryPage to host the "Neuer Eintrag" button. Kept as
   *  a slot (not a baked-in button) so this component stays page-agnostic. */
  actions?: ReactNode;
}

export function VaultHeader({ noteCount, agentCount, actions }: VaultHeaderProps) {
  return (
    <div className="flex items-start justify-between mb-8">
      <div>
        {/* Title with blinking cursor */}
        <h1
          className="font-bold tracking-tight leading-none"
          style={{
            fontSize: "clamp(2rem, 5vw, 3rem)",
            color: "var(--color-text-primary)",
          }}
        >
          memory
          <span className="vault-cursor" aria-hidden="true">_</span>
        </h1>

        {/* Subtitle — stats line */}
        <p
          className="font-mono uppercase tracking-wider mt-2"
          style={{
            fontSize: "11px",
            color: "var(--color-text-muted)",
            letterSpacing: "0.08em",
          }}
        >
          {noteCount.toLocaleString()} NOTES · {agentCount} AGENTS · VAULT@~/.MC
        </p>
      </div>

      {/* Action slot + keyboard shortcut hint, right-aligned. */}
      <div className="flex items-center gap-3 mt-1">
        {actions}
        <div
          className="font-mono text-xs"
          style={{ color: "var(--color-text-muted)" }}
        >
          ⌘K
        </div>
      </div>

      {/* Blinking cursor keyframe */}
      <style>{`
        @keyframes vault-blink {
          0%, 100% { opacity: 1; }
          50% { opacity: 0; }
        }
        .vault-cursor {
          display: inline-block;
          animation: vault-blink 1s step-end infinite;
          color: ${C.accent};
          margin-left: 1px;
        }
        @media (prefers-reduced-motion: reduce) {
          .vault-cursor { animation: none; opacity: 1; }
        }
      `}</style>
    </div>
  );
}
