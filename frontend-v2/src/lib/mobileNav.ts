/**
 * Phone navigation V2 „Schnell-Knopf" (operator decision 2026-10-01).
 *
 *   Home · Tasks · ⊕ New · Chats · Inbox
 *
 * Five places, always icon + word (Apple HIG / Material: 3–5 destinations,
 * never drop the labels). ⊕ is the one action in the bar. It opens the menu
 * sheet, variant B „Zwei Ebenen" (operator decision 2026-10-02):
 *
 *   level 1 — New job · Voice · last chat · Pages › · Settings ›
 *   level 2 — Pages: every destination not in the bar, from lib/nav.ts
 *
 * Pure data, no React; labels are `nav.*` keys.
 */
import {
  FolderKanban,
  Home,
  Inbox,
  MessagesSquare,
  type LucideIcon,
} from "lucide-react";
import { CHROME_ITEMS, NAV_ITEMS, type NavItem } from "./nav";

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

/** The route each tab stands for (Chats = the sessions page). */
export const TAB_HREFS: Record<MobileTabKey, string> = {
  home: "/",
  tasks: "/tasks",
  chats: "/sessions",
  inbox: "/inbox",
};

export type SheetPages = { usedMost: NavItem[]; others: NavItem[] };

/**
 * Level 2 of the ⊕ sheet: every destination in NAV_ITEMS that is neither a
 * tab nor chrome (Settings has its own row on level 1). Generated, never a
 * second list — a page added to nav.ts shows up here, and a vertical that is
 * switched off (Benchmark) disappears here exactly like in the sidebar.
 *
 * "Used most" = pages with a `phoneRank`, in rank order; "Others" = the rest,
 * alphabetical by `labelOf` (the caller passes the translated label).
 */
export function sheetPages(
  labelOf: (item: NavItem) => string = (i) => i.label,
  items: NavItem[] = NAV_ITEMS,
): SheetPages {
  const skip = new Set<string>([...Object.values(TAB_HREFS), ...CHROME_ITEMS]);
  const pages = items.filter((i) => !skip.has(i.href));
  const usedMost = pages
    .filter((i) => i.phoneRank !== undefined)
    .sort((a, b) => (a.phoneRank ?? 0) - (b.phoneRank ?? 0));
  const others = pages
    .filter((i) => i.phoneRank === undefined)
    .sort((a, b) => labelOf(a).localeCompare(labelOf(b)));
  return { usedMost, others };
}
