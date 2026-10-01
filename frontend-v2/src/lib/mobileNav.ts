/**
 * Phone navigation V2 „Schnell-Knopf" (operator decision 2026-10-01).
 *
 *   Home · Tasks · ⊕ New · Chats · Inbox
 *
 * Five places, always icon + word (Apple HIG / Material: 3–5 destinations,
 * never drop the labels). ⊕ is the one action in the bar: it opens a sheet
 * with "New job", voice, the last chats and every other area — which is why
 * there is no "More" tab. Pure data, no React; labels are `nav.*` keys.
 */
import {
  Bot,
  Brain,
  Calendar,
  FolderKanban,
  Home,
  Inbox,
  MessagesSquare,
  MoreHorizontal,
  Server,
  TrendingUp,
  type LucideIcon,
} from "lucide-react";

export type MobileTabKey = "home" | "tasks" | "chats" | "inbox";

/** Which tab is lit for a route; `null` for every place the ⊕ sheet leads to. */
export function activeTabFor(pathname: string): MobileTabKey | null {
  if (pathname === "/") return "home";
  if (pathname.startsWith("/tasks")) return "tasks";
  if (pathname.startsWith("/sessions")) return "chats";
  if (pathname.startsWith("/inbox")) return "inbox";
  return null;
}

export const TAB_ICONS: Record<MobileTabKey, LucideIcon> = {
  home: Home,
  tasks: FolderKanban,
  chats: MessagesSquare,
  inbox: Inbox,
};

/** Label keys in the `nav` namespace. "Chats" is the sessions page (route unchanged). */
export const TAB_LABEL_KEYS: Record<MobileTabKey, string> = {
  home: "home",
  tasks: "tasks",
  chats: "sessions",
  inbox: "inbox",
};

/**
 * "All areas" tiles in the ⊕ sheet. `href: null` = the full menu (board,
 * account, every route) — the former "Index" drawer.
 */
export const AREA_TILES: { key: string; href: string | null; icon: LucideIcon; labelKey: string }[] = [
  { key: "runtimes", href: "/runtimes", icon: Server, labelKey: "runtimes" },
  { key: "insights", href: "/insights", icon: TrendingUp, labelKey: "insights" },
  { key: "agents", href: "/agents", icon: Bot, labelKey: "agents" },
  { key: "memory", href: "/memory", icon: Brain, labelKey: "memory" },
  { key: "schedule", href: "/schedule", icon: Calendar, labelKey: "schedule" },
  { key: "more", href: null, icon: MoreHorizontal, labelKey: "quick.more" },
];
