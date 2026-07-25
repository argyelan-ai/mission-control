"use client";

/**
 * EntityIcon — v3: ersetzt Emoji-Glyphen durch Lucide-SVG-Icons.
 *
 * Agenten-/Board-/Skill-Identitäten kommen als Emoji-String aus der DB
 * (Legacy). Diese Map übersetzt sie in die eckige SVG-Welt des Leitstands —
 * Variationsselektoren (U+FE0F) und Hauttöne (U+1F3FB–FF) werden vor dem
 * Lookup abgestreift. Unbekannte/fehlende Werte fallen auf `Bot` zurück.
 * Neue Board-Icons können auch direkt als Schlüssel ("rocket") gespeichert
 * werden — der Lookup akzeptiert beides.
 */

import {
  Bot, Zap, PenLine, Mic, Palette, HardHat, Search, Rocket, Wrench,
  ShieldCheck, FlaskConical, Brain, Lightbulb, Flame, Package, Globe,
  Target, Satellite, Puzzle, BarChart3, Newspaper, Folder, Settings,
  Eye, Radio, Microscope, Clapperboard, TrendingUp, MessageSquare,
  Lock, Calendar, Bell, Star, HardDrive, Monitor, Compass, NotebookPen,
  Sparkles, Sun, CalendarDays, Paintbrush, Stethoscope, RefreshCw,
  Pin, KeyRound, MoonStar, type LucideIcon,
} from "lucide-react";

// ── Emoji → Icon (DB-Realität + sinnvolle Deckung) ──────────────────────────

const EMOJI_TO_ICON: Record<string, LucideIcon> = {
  "🤖": Bot,
  "⚡": Zap,
  "✍️": PenLine,
  "✍": PenLine,
  "🖋": PenLine,
  "📝": NotebookPen,
  "🎙️": Mic,
  "🎙": Mic,
  "🎤": Mic,
  "🎨": Palette,
  "🏗️": HardHat,
  "🏗": HardHat,
  "🔍": Search,
  "🚀": Rocket,
  "🛠️": Wrench,
  "🛠": Wrench,
  "🔧": Wrench,
  "🛡️": ShieldCheck,
  "🛡": ShieldCheck,
  "🧪": FlaskConical,
  "🧠": Brain,
  "💡": Lightbulb,
  "🔥": Flame,
  "📦": Package,
  "🌍": Globe,
  "🎯": Target,
  "🛰️": Satellite,
  "🛰": Satellite,
  "🧩": Puzzle,
  "📊": BarChart3,
  "📈": TrendingUp,
  "🗞️": Newspaper,
  "🗞": Newspaper,
  "📰": Newspaper,
  "📁": Folder,
  "⚙️": Settings,
  "⚙": Settings,
  "👁️": Eye,
  "👁": Eye,
  "👀": Eye,
  "📡": Radio,
  "🔬": Microscope,
  "🎬": Clapperboard,
  "💬": MessageSquare,
  "🔒": Lock,
  "📅": Calendar,
  "🔔": Bell,
  "⭐": Star,
  "💾": HardDrive,
  "🖥️": Monitor,
  "🖥": Monitor,
  "🧭": Compass,
  "✨": Sparkles,
  "☀️": Sun,
  "☀": Sun,
  "🗓️": CalendarDays,
  "🗓": CalendarDays,
  "🧹": Paintbrush,
  "🩺": Stethoscope,
  "🔄": RefreshCw,
  "💤": MoonStar,
  "📌": Pin,
  "🔑": KeyRound,
};

// ── Schlüssel → Icon (für neue, emoji-freie Speicherung) ────────────────────

const KEY_TO_ICON: Record<string, LucideIcon> = {
  bot: Bot,
  zap: Zap,
  pen: PenLine,
  mic: Mic,
  palette: Palette,
  hardhat: HardHat,
  search: Search,
  rocket: Rocket,
  wrench: Wrench,
  shield: ShieldCheck,
  flask: FlaskConical,
  brain: Brain,
  lightbulb: Lightbulb,
  flame: Flame,
  package: Package,
  globe: Globe,
  target: Target,
  satellite: Satellite,
  puzzle: Puzzle,
  chart: BarChart3,
  news: Newspaper,
  folder: Folder,
  settings: Settings,
  eye: Eye,
  radio: Radio,
  microscope: Microscope,
  clapperboard: Clapperboard,
  message: MessageSquare,
  lock: Lock,
  calendar: Calendar,
  bell: Bell,
  star: Star,
  drive: HardDrive,
  monitor: Monitor,
  compass: Compass,
  sparkles: Sparkles,
};

/** Auswahl für Icon-Picker (stabile Reihenfolge) */
export const ENTITY_ICON_KEYS = Object.keys(KEY_TO_ICON);

function normalize(raw: string): string {
  // Variationsselektor U+FE0F, Hauttöne U+1F3FB–U+1F3FF, ZWJ-Bestandteile weg
  return raw
    .replace(/️/g, "")
    .replace(/[\u{1F3FB}-\u{1F3FF}]/gu, "")
    .trim();
}

export function resolveEntityIcon(value?: string | null): LucideIcon {
  if (!value) return Bot;
  const key = value.trim().toLowerCase();
  if (KEY_TO_ICON[key]) return KEY_TO_ICON[key];
  const emoji = normalize(value);
  return EMOJI_TO_ICON[emoji] ?? Bot;
}

interface EntityIconProps {
  /** Emoji (Legacy-DB) oder Schlüssel ("rocket") */
  value?: string | null;
  size?: number;
  className?: string;
  style?: React.CSSProperties;
  /** Überschreibt die Farbe (Default: currentColor) */
  color?: string;
  strokeWidth?: number;
}

export function EntityIcon({
  value,
  size = 16,
  className,
  style,
  color,
  strokeWidth = 1.75,
}: EntityIconProps) {
  const Icon = resolveEntityIcon(value);
  return (
    <Icon
      size={size}
      strokeWidth={strokeWidth}
      className={className}
      style={{ color, flexShrink: 0, ...style }}
      aria-hidden
    />
  );
}
