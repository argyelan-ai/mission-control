"use client";

/**
 * HarnessIcon — SVG-Marken für CLI-Harnesses statt ausgeschriebener Namen.
 *
 * Claude/Grok bekommen eigens gezeichnete geometrische Marken (angefertigt
 * für den Leitstand, keine kopierten Logos); interne Harnesses nutzen
 * Lucide-Icons. Die Label-Map bleibt die Quelle für Tooltips/aria.
 */

import { Terminal, Boxes, AudioWaveform, type LucideIcon } from "lucide-react";
import { HARNESS_LABELS, HOST_HARNESS_LABELS, type Harness, type HostHarness } from "@/lib/types";

export type AnyHarness = Harness | HostHarness;

export function harnessLabel(harness: string): string {
  return (
    HARNESS_LABELS[harness as Harness] ??
    HOST_HARNESS_LABELS[harness as HostHarness] ??
    harness
  );
}

// ── Eigene Marken (24×24, stroke-basiert wie Lucide) ────────────────────────

function ClaudeMark({ size, strokeWidth = 1.75, ...rest }: MarkProps) {
  // 8-strahlige Sternmarke (Claude/Anthropic-Familie), eigens gezeichnet
  const rays = [
    [12, 2, 12, 22],
    [2, 12, 22, 12],
    [5, 5, 19, 19],
    [19, 5, 5, 19],
  ];
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth={strokeWidth} strokeLinecap="round" aria-hidden {...rest}>
      {rays.map(([x1, y1, x2, y2], i) => (
        <line key={i} x1={x1} y1={y1} x2={x2} y2={y2} />
      ))}
      <circle cx="12" cy="12" r="2.4" fill="currentColor" stroke="none" />
    </svg>
  );
}

function GrokMark({ size, strokeWidth = 1.75, ...rest }: MarkProps) {
  // Gebrochenes X (xAI/Grok-Familie), eigens gezeichnet
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth={strokeWidth} strokeLinecap="round" aria-hidden {...rest}>
      <path d="M5 4 L19 20" />
      <path d="M19 4 L13.5 11.5" />
      <path d="M10.5 12.5 L5 20" />
    </svg>
  );
}

type MarkProps = {
  size: number;
  strokeWidth?: number;
  className?: string;
  style?: React.CSSProperties;
};

const LUCIDE_MARKS: Partial<Record<AnyHarness, LucideIcon>> = {
  omp: Terminal,
  openclaude: Boxes,
  hermes: AudioWaveform,
};

const CUSTOM_MARKS: Partial<Record<AnyHarness, React.ComponentType<MarkProps>>> = {
  claude: ClaudeMark,
  grok: GrokMark,
};

interface HarnessIconProps {
  harness: string;
  size?: number;
  className?: string;
  style?: React.CSSProperties;
  strokeWidth?: number;
}

export function HarnessIcon({ harness, size = 12, className, style, strokeWidth = 1.75 }: HarnessIconProps) {
  const Custom = CUSTOM_MARKS[harness as AnyHarness];
  if (Custom) {
    return <Custom size={size} strokeWidth={strokeWidth} className={className} style={style} />;
  }
  const LucideMark = LUCIDE_MARKS[harness as AnyHarness] ?? Terminal;
  return (
    <LucideMark
      size={size}
      strokeWidth={strokeWidth}
      className={className}
      style={{ flexShrink: 0, ...style }}
      aria-hidden
    />
  );
}
