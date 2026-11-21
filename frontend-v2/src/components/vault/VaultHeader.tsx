"use client";

/**
 * VaultHeader — editorial masthead for the memory page.
 * title: the page's nav word (nav.memory) in Clash Display (.display) with a
 * blinking underscore cursor — same word as the menu, in every language.
 * subtitle: stats line. No kicker above the title (DESIGN.md K7) and no
 * machine path behind the stats.
 */

import type { ReactNode } from "react";
import { useLocale, useTranslations } from "next-intl";
import { C } from "@/lib/colors";

interface VaultHeaderProps {
  noteCount: number;
  /** True when noteCount is the search's result cap — shown as "100+". */
  noteCountCapped?: boolean;
  agentCount: number;
  /** Optional action slot rendered on the right side of the header.
   *  Used by VaultMemoryPage to host the "Neuer Eintrag" button. Kept as
   *  a slot (not a baked-in button) so this component stays page-agnostic. */
  actions?: ReactNode;
}

export function VaultHeader({ noteCount, noteCountCapped = false, agentCount, actions }: VaultHeaderProps) {
  const t = useTranslations("vault");
  const tNav = useTranslations("nav");
  const locale = useLocale();
  const notes = `${new Intl.NumberFormat(locale).format(noteCount)}${noteCountCapped ? "+" : ""}`;
  return (
    <div className="flex items-start justify-between mb-8">
      <div>
        {/* Title with blinking cursor */}
        <h1
          className="display font-semibold leading-none"
          style={{
            fontSize: "clamp(1.5rem, 3.5vw, 1.75rem)",
            color: "var(--color-text-primary)",
          }}
        >
          {tNav("memory")}
          <span className="vault-cursor" aria-hidden="true">_</span>
        </h1>

        {/* Subtitle — stats line (mono meta voice, secondary tone) */}
        <p
          className="font-mono uppercase tracking-wider mt-2"
          style={{
            fontSize: "11px",
            color: "var(--color-text-secondary)",
            letterSpacing: "0.08em",
          }}
        >
          {t("headerStats", {
            notes,
            notesCount: noteCountCapped ? noteCount + 1 : noteCount,
            agents: agentCount,
          })}
        </p>
      </div>

      {/* Action slot + keyboard shortcut hint, right-aligned. The hint is
          desktop-only: phones have no ⌘ key. */}
      <div className="flex items-center gap-3 mt-1">
        {actions}
        <div
          className="hidden md:block font-mono text-xs"
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
