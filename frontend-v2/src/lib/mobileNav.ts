/**
 * Phone navigation V2 „Schnell-Knopf" (operator decision 2026-10-01).
 *
 *   Home · Tasks · ⊕ New · Chats · Inbox
 *
 * Five places, always icon + word (Apple HIG / Material: 3–5 destinations,
 * never drop the labels). ⊕ is the one action in the bar: it opens a sheet
 * with "New job", voice, the last chats, every other area and the account —
 * it is the one phone menu, which is why there is no "More" tab. Pure data,
 * no React; labels are `nav.*` keys.
 */
import { FolderKanban, Home, Inbox, MessagesSquare, type LucideIcon } from "lucide-react";
import { CHROME_ITEMS, NAV_TREE, navItem, type NavGroup, type NavItem } from "./nav";

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

/** The route each tab stands for (Chats may carry `?agent=…`; the route is /sessions). */
export const TAB_ROUTES: Record<MobileTabKey, string> = {
  home: "/",
  tasks: "/tasks",
  chats: "/sessions",
  inbox: "/inbox",
};

/**
 * "All areas" in the ⊕ sheet: every group of the one nav tree (lib/nav.ts),
 * minus the routes the tab bar already carries. Derived, never hand-listed —
 * a destination added to nav.ts shows up on the phone by itself, and a
 * feature-flagged one (Benchmark) follows the same flag as the sidebar.
 * Emptied groups disappear.
 */
export function sheetAreaGroups(tree: NavGroup[] = NAV_TREE): NavGroup[] {
  const inBar = new Set(Object.values(TAB_ROUTES));
  return tree
    .map((g) => ({ ...g, children: g.children.filter((c) => !inBar.has(c.href)) }))
    .filter((g) => g.children.length > 0);
}

/** Account-level destinations (Settings) — the sheet's account section, as on the desktop column. */
export function sheetAccountLinks(): NavItem[] {
  return CHROME_ITEMS.map((href) => navItem(href)).filter((i): i is NavItem => !!i);
}
