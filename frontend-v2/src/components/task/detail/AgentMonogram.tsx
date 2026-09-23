"use client";

import { C } from "@/lib/colors";

/** "alpha" → "AL", "code reviewer" → "CR". */
export function monogramOf(name: string): string {
  const words = name.trim().split(/[\s_\-.]+/).filter(Boolean);
  if (words.length >= 2) return (words[0][0] + words[1][0]).toUpperCase();
  return (words[0] ?? "?").slice(0, 2).toUpperCase();
}

/**
 * Agents have no colour of their own (colour means status) — they get a grey
 * two-letter monogram.
 */
export function AgentMonogram({ name, size = 16 }: { name: string; size?: number }) {
  return (
    <span
      aria-hidden
      className="inline-flex items-center justify-center rounded-full font-mono font-medium shrink-0"
      style={{
        width: size,
        height: size,
        fontSize: Math.max(8, Math.round(size * 0.5)),
        background: C.bgHover,
        color: C.textSecondary,
        border: `1px solid ${C.border}`,
      }}
    >
      {monogramOf(name)}
    </span>
  );
}
