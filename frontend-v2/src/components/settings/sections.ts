/**
 * The Settings section registry — one list, read by the desktop side nav and
 * by the phone section list, so a section can never exist on one and be
 * missing on the other.
 *
 * Lives outside `app/settings/page.tsx` because a Next.js page module may only
 * export the page itself; tests import this list directly.
 */
import {
  User,
  Shield,
  Users,
  Key,
  KeyRound,
  Github,
  Zap,
  SlidersHorizontal,
  Keyboard,
  Info,
  DollarSign,
  MessageSquare,
  Send,
  BrainCircuit,
  Moon,
  Palette,
  type LucideIcon,
} from "lucide-react";

// labelKey pattern (docs/i18n.md): keys resolve via t() at the render site —
// never store translated strings in module constants.
// Thirteen entries in one flat list put "change my password" next to "how much
// autonomy do agents have" next to "where does the Slack token live". The
// groups answer one question each: whose account, how the fleet behaves, what
// it talks to, what secrets it holds, who administers it.
export type SettingsGroup = "account" | "fleet" | "connections" | "secrets" | "system";

export const GROUP_ORDER: SettingsGroup[] = ["account", "fleet", "connections", "secrets", "system"];

export interface SettingsSection {
  id: string;
  labelKey: string;
  icon: LucideIcon;
  group: SettingsGroup;
  adminOnly?: boolean;
}

export const SECTIONS: SettingsSection[] = [
  { id: "profile", labelKey: "sections.profile", icon: User, group: "account" },
  { id: "security", labelKey: "sections.security", icon: Shield, group: "account" },
  { id: "appearance", labelKey: "sections.appearance", icon: Palette, group: "account" },
  { id: "shortcuts", labelKey: "sections.shortcuts", icon: Keyboard, group: "account" },
  { id: "autonomy", labelKey: "sections.autonomy", icon: SlidersHorizontal, group: "fleet", adminOnly: true },
  { id: "intelligence", labelKey: "sections.intelligence", icon: Zap, group: "fleet", adminOnly: true },
  { id: "costs", labelKey: "sections.costs", icon: DollarSign, group: "fleet", adminOnly: true },
  { id: "night-shift", labelKey: "sections.nightShift", icon: Moon, group: "fleet", adminOnly: true },
  { id: "github", labelKey: "sections.github", icon: Github, group: "connections", adminOnly: true },
  { id: "slack", labelKey: "sections.slack", icon: MessageSquare, group: "connections", adminOnly: true },
  { id: "telegram", labelKey: "sections.telegram", icon: Send, group: "connections", adminOnly: true },
  { id: "ai-providers", labelKey: "sections.aiProviders", icon: BrainCircuit, group: "connections", adminOnly: true },
  { id: "apikeys", labelKey: "sections.apikeys", icon: Key, group: "secrets", adminOnly: true },
  { id: "credentials", labelKey: "sections.credentials", icon: KeyRound, group: "secrets", adminOnly: true },
  { id: "users", labelKey: "sections.users", icon: Users, group: "system", adminOnly: true },
  { id: "about", labelKey: "sections.about", icon: Info, group: "system" },
];

/** The URL of one section — the same link on the phone and the desktop. */
export function sectionHref(id: string): string {
  return `/settings?section=${id}`;
}
