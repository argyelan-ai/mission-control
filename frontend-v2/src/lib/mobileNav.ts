/**
 * Phone navigation „Drei Daumen" (operator decision 2026-11-18, ADR-102
 * principle 1: "few menus: chat, models, knowledge"):
 *
 *   Chat · Models · Knowledge
 *
 * Three places, always icon + word. Chat lands in the last chat (the sessions
 * page restores it); every chat is one tap away in the drawer behind the
 * sidebar icon of the chat header (components/layout/ChatDrawer.tsx). Every
 * other page left the main navigation and sits on the More page (/more), generated
 * by `sheetPages` below — nothing was deleted, every route still works.
 * Frozen pages (nav.ts `FROZEN_ITEMS`, Etappe 2) are not offered there at all.
 *
 * Pure data, no React; labels are `cockpit.*` keys.
 */
import { BookOpen, Gauge, MessageSquare, type LucideIcon } from "lucide-react";
import { CHROME_ITEMS, FROZEN_ITEMS, NAV_ITEMS, type NavItem } from "./nav";

import { chatUrl } from "./chatHistory";
import type { ChatRef } from "./recentChats";

/** The More page: every page that left the main navigation. */
export const MORE_HREF = "/more";

export type MobileTabKey = "chat" | "models" | "knowledge";

export const TAB_ORDER: MobileTabKey[] = ["chat", "models", "knowledge"];

/** Which tab is lit for a route; `null` for every page reached via More. */
export function activeTabFor(pathname: string): MobileTabKey | null {
  if (pathname === "/" || pathname.startsWith("/sessions")) return "chat";
  if (pathname.startsWith("/runtimes") || pathname.startsWith("/bench")) return "models";
  if (pathname.startsWith("/knowledge") || pathname.startsWith("/memory") || pathname.startsWith("/skills")) {
    return "knowledge";
  }
  return null;
}

export const TAB_ICONS: Record<MobileTabKey, LucideIcon> = {
  chat: MessageSquare,
  models: Gauge,
  knowledge: BookOpen,
};

/** Label keys in the `cockpit` namespace. */
export const TAB_LABEL_KEYS: Record<MobileTabKey, string> = {
  chat: "tabChat",
  models: "tabModels",
  knowledge: "tabKnowledge",
};

/** The route each tab stands for: the chat page, the runtimes page, the knowledge landing. */
export const TAB_HREFS: Record<MobileTabKey, string> = {
  chat: "/sessions",
  models: "/runtimes",
  knowledge: "/knowledge",
};

/**
 * Where the Chat tab leads: straight into the last chat (its plain URL, no
 * entry marker — no list goes in behind it). Plain `/sessions` sent an open
 * chat back to the old full-screen list (shell review A2).
 */
export function chatTabHref(recent: ChatRef[]): string {
  return recent[0] ? chatUrl(recent[0].kind, recent[0].id) : TAB_HREFS.chat;
}

export type SheetPages = { usedMost: NavItem[]; others: NavItem[] };

/**
 * The More page (/more): every destination in NAV_ITEMS that is neither a
 * tab, nor a page under a tab (Memory, Skills), nor chrome (Settings
 * itself) nor frozen (Etappe 2). Generated, never a
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
  const skip = new Set<string>([
    ...Object.values(TAB_HREFS),
    ...CHROME_ITEMS,
    ...FROZEN_ITEMS.map((i) => i.href),
  ]);
  // Memory and Skills sit as links on the Knowledge landing, so they are not
  // offered a second time. Benchmark lights the Models tab (`activeTabFor`)
  // but stays here on purpose: Models links to it only from a runtime's bench
  // card, which is not always shown — More is its one sure way in on the phone.
  const underTab = (href: string) => activeTabFor(href) === "knowledge";
  const pages = items.filter((i) => !skip.has(i.href) && !underTab(i.href));
  const usedMost = pages
    .filter((i) => i.phoneRank !== undefined)
    .sort((a, b) => (a.phoneRank ?? 0) - (b.phoneRank ?? 0));
  const others = pages
    .filter((i) => i.phoneRank === undefined)
    .sort((a, b) => labelOf(a).localeCompare(labelOf(b)));
  return { usedMost, others };
}
