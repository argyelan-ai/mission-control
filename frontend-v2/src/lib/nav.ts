/**
 * Navigation model — Shell v4 „Eine Spalte".
 *
 * Single source for what the shell can navigate to. Split into a flat list
 * (command palette, deep links) and a nestable tree (sidebar). The sidebar
 * shows a user-chosen set of pinned leaves first; everything not pinned stays
 * reachable one click deep inside its group.
 *
 * Pure data + pure functions — no React, no store. Labels are i18n keys under
 * the "nav" namespace; the components resolve them with useTranslations.
 * Tested in __tests__/nav.test.ts.
 */
import {
  Home,
  FolderKanban,
  Bot,
  Inbox,
  Calendar,
  Settings,
  TrendingUp,
  Brain,
  PenLine,
  Puzzle,
  FolderGit2,
  Server,
  Terminal,
  Building2,
  Newspaper,
  FolderOpen,
  Repeat,
  FlaskConical,
  Compass,
  Library,
  Clapperboard,
  Cpu,
  type LucideIcon,
} from "lucide-react";
import { VERTICALS } from "./verticals";

export type NavItem = {
  href: string;
  icon: LucideIcon;
  /** Fallback label; the shell renders t(labelKey) and falls back to this. */
  label: string;
  labelKey: string;
};

export type NavGroup = {
  key: string;
  icon: LucideIcon;
  label: string;
  /** Caps section label — MobileNav's menu still renders groups as headings. */
  labelKey: string;
  /** Sentence-case label for the sidebar's clickable group row. */
  rowLabelKey: string;
  children: NavItem[];
};

/** Every destination the shell knows, flat. */
export const NAV_ITEMS: NavItem[] = [
  { href: "/", icon: Home, label: "Home", labelKey: "home" },
  { href: "/tasks", icon: FolderKanban, label: "Tasks", labelKey: "tasks" },
  { href: "/agents", icon: Bot, label: "Agents", labelKey: "agents" },
  { href: "/office", icon: Building2, label: "Office", labelKey: "office" },
  { href: "/inbox", icon: Inbox, label: "Inbox", labelKey: "inbox" },
  { href: "/insights", icon: TrendingUp, label: "Insights", labelKey: "insights" },
  { href: "/memory", icon: Brain, label: "Memory", labelKey: "memory" },
  { href: "/files", icon: FolderOpen, label: "Files", labelKey: "files" },
  // News-Studio vertical — stripped from the public-release build
  ...(VERTICALS.newsStudio
    ? [
        { href: "/content", icon: PenLine, label: "Content", labelKey: "content" },
        { href: "/news", icon: Newspaper, label: "News", labelKey: "news" },
      ]
    : []),
  // Benchmark-Studio vertical — strippable (flag flipped by release script)
  ...(VERTICALS.benchStudio
    ? [{ href: "/bench", icon: FlaskConical, label: "Benchmark", labelKey: "bench" }]
    : []),
  { href: "/repos", icon: FolderGit2, label: "Repos", labelKey: "repos" },
  { href: "/skills", icon: Puzzle, label: "Skills", labelKey: "skills" },
  { href: "/runtimes", icon: Server, label: "Runtimes", labelKey: "runtimes" },
  { href: "/sessions", icon: Terminal, label: "Sessions", labelKey: "sessions" },
  { href: "/loops", icon: Repeat, label: "Loops", labelKey: "loops" },
  { href: "/schedule", icon: Calendar, label: "Schedule", labelKey: "schedule" },
  { href: "/settings", icon: Settings, label: "Settings", labelKey: "settings" },
];

const byHref = new Map(NAV_ITEMS.map((i) => [i.href, i]));

/** Look a destination up by route. */
export function navItem(href: string): NavItem | undefined {
  return byHref.get(href);
}

const pick = (hrefs: string[]): NavItem[] =>
  hrefs.map((h) => byHref.get(h)).filter((i): i is NavItem => !!i);

/**
 * The nestable tree. Groups collapse into a single sidebar row that expands
 * in place — the reason 19 rows fit in far fewer without hiding anything.
 * Group keys and labelKeys match the pre-v4 NAV_GROUPS, so the existing
 * translations keep working.
 */
export const NAV_TREE: NavGroup[] = [
  {
    key: "overview",
    icon: Compass,
    label: "OVERVIEW",
    labelKey: "groupOverview",
    rowLabelKey: "navGroupOverview",
    children: pick(["/", "/insights", "/office"]),
  },
  {
    key: "work",
    icon: FolderKanban,
    label: "WORK",
    labelKey: "groupWork",
    rowLabelKey: "navGroupWork",
    children: pick(["/tasks", "/inbox", "/sessions", "/agents"]),
  },
  {
    key: "knowledge",
    icon: Library,
    label: "KNOWLEDGE",
    labelKey: "groupKnowledge",
    rowLabelKey: "navGroupKnowledge",
    children: pick(["/memory", "/files", "/repos", "/skills"]),
  },
  {
    key: "studio",
    icon: Clapperboard,
    label: "STUDIO",
    labelKey: "groupStudio",
    rowLabelKey: "navGroupStudio",
    children: pick(["/content", "/news", "/bench"]),
  },
  {
    key: "system",
    icon: Cpu,
    label: "SYSTEM",
    labelKey: "groupSystem",
    rowLabelKey: "navGroupSystem",
    children: pick(["/runtimes", "/loops", "/schedule", "/settings"]),
  },
];

/** Startbelegung — overridable per user, persisted in the app store. */
export const DEFAULT_PINS: string[] = [
  "/",
  "/tasks",
  "/sessions",
  "/agents",
  "/inbox",
  "/memory",
];

export type ResolvedNav = {
  /** Pinned leaves, in the user's order. Unknown routes are dropped. */
  pinned: NavItem[];
  /** Groups minus everything already pinned. Emptied groups disappear. */
  groups: NavGroup[];
};

/**
 * Split the tree into "pinned rows" and "the rest, still grouped".
 * A pinned destination never appears twice.
 *
 * A missing pin list is treated as "nothing pinned" rather than an error:
 * state persisted by an older build carries no pinnedNav, and callers should
 * get the full tree instead of a crash.
 */
export function resolveNav(pins: string[] | undefined | null): ResolvedNav {
  const pinned = (pins ?? [])
    .map((href) => byHref.get(href))
    .filter((i): i is NavItem => !!i);

  const pinnedHrefs = new Set(pinned.map((i) => i.href));

  const groups = NAV_TREE.map((g) => ({
    ...g,
    children: g.children.filter((c) => !pinnedHrefs.has(c.href)),
  })).filter((g) => g.children.length > 0);

  return { pinned, groups };
}

/** True when `href` is the route currently shown. Root only matches exactly. */
export function isActiveRoute(href: string, pathname: string): boolean {
  return href === "/" ? pathname === "/" : pathname.startsWith(href);
}

/** The group a route lives in — used to auto-open the right group on load. */
export function groupKeyFor(href: string): string | undefined {
  return NAV_TREE.find((g) => g.children.some((c) => c.href === href))?.key;
}
