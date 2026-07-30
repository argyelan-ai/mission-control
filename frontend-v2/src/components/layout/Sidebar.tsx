"use client";

import { usePathname, useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import Link from "next/link";
import { motion, AnimatePresence } from "framer-motion";
import {
  Home,
  FolderKanban,
  Bot,
  Inbox,
  Calendar,
  LogOut,
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
  type LucideIcon,
} from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { useAppStore } from "@/lib/store";
import { clearToken, api } from "@/lib/api";
import type { Approval } from "@/lib/types";
import { VoiceButton } from "@/components/voice/VoiceWidget";
import { P2 } from "@/lib/colors";
import { VERTICALS } from "@/lib/verticals";

// Wordmark: env-getrieben — Deployments branden via NEXT_PUBLIC_BRAND
// ("main.accent"-Split am letzten Punkt; Default = Produktname).
const _BRAND = process.env.NEXT_PUBLIC_BRAND || "Mission.Control";
const _dot = _BRAND.lastIndexOf(".");
const BRAND_MAIN = _dot > 0 ? _BRAND.slice(0, _dot) : _BRAND;
const BRAND_ACCENT = _dot > 0 ? _BRAND.slice(_dot) : "";

// label = English fallback; labelKey = message key in the "nav" namespace
// (messages/en.json + de.json). Render sites translate via t(labelKey).
export type NavItem = { href: string; icon: LucideIcon; label: string; labelKey: string };

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

// P2: Gruppen priorisieren den Alltag (S5 aus 00-redesign-brief: Home/Tasks/
// Sessions/Agents sind der Alltag, Rest ist selten). Reihenfolge der Gruppen
// spiegelt Nutzungshäufigkeit — bewusste Abweichung von der alten Liste.
const _byHref = new Map(NAV_ITEMS.map((i) => [i.href, i]));
const pick = (hrefs: string[]): NavItem[] =>
  hrefs.map((h) => _byHref.get(h)).filter((i): i is NavItem => !!i);

export const NAV_GROUPS: { label: string; labelKey: string; items: NavItem[] }[] = [
  { label: "OVERVIEW", labelKey: "groupOverview", items: pick(["/", "/insights", "/office"]) },
  { label: "WORK", labelKey: "groupWork", items: pick(["/tasks", "/inbox", "/sessions", "/agents"]) },
  { label: "KNOWLEDGE", labelKey: "groupKnowledge", items: pick(["/memory", "/files", "/repos", "/skills"]) },
  { label: "STUDIO", labelKey: "groupStudio", items: pick(["/content", "/news", "/bench"]) },
  { label: "SYSTEM", labelKey: "groupSystem", items: pick(["/runtimes", "/loops", "/schedule", "/settings"]) },
];

const MONO = { fontFamily: "var(--font-p2-mono)" };

export default function Sidebar() {
  const t = useTranslations("nav");
  const pathname = usePathname();
  const router = useRouter();
  const { sidebarCollapsed, currentUser } = useAppStore();

  const { data: approvals } = useQuery<Approval[]>({
    queryKey: ["approvals-badge"],
    queryFn: () => api.approvals.list(),
    refetchInterval: 30_000,
  });
  const hasPendingApprovals = (approvals ?? []).some((a) => a.status === "pending");

  function handleLogout() {
    clearToken();
    router.replace("/login");
  }

  const sidebarWidth = sidebarCollapsed ? 48 : 240;

  return (
    <motion.aside
      animate={{ width: sidebarWidth }}
      transition={{ duration: 0.2, ease: [0.16, 1, 0.3, 1] }}
      className="flex flex-col h-full overflow-hidden shrink-0"
      style={{
        backgroundColor: "var(--color-p2-pan)",
        borderRight: "1px solid var(--color-p2-line2)",
      }}
    >
      {/* Logo area — Space-Mono-Wordmark, Akzent */}
      <div
        className="shrink-0 flex items-center gap-3 px-3 h-14"
        style={{ borderBottom: "1px solid var(--color-p2-line2)" }}
      >
        <AnimatePresence initial={false} mode="wait">
          {sidebarCollapsed ? (
            <motion.span
              key="short"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              transition={{ duration: 0.12 }}
              style={{
                color: "var(--color-p2-txt)",
                fontFamily: "var(--font-p2-display)",
                fontWeight: 700,
                fontSize: "14px",
                letterSpacing: "0.02em",
              }}
            >
              M<span style={{ color: P2.amb }}>/</span>
            </motion.span>
          ) : (
            <motion.span
              key="full"
              initial={{ opacity: 0, width: 0 }}
              animate={{ opacity: 1, width: "auto" }}
              exit={{ opacity: 0, width: 0 }}
              transition={{ duration: 0.15 }}
              className="whitespace-nowrap overflow-hidden"
              style={{
                color: "var(--color-p2-txt)",
                fontFamily: "var(--font-p2-display)",
                fontWeight: 700,
                fontSize: "15px",
                letterSpacing: "0.02em",
              }}
            >
              {BRAND_MAIN}
              <span style={{ color: P2.amb }}>{BRAND_ACCENT}</span>
            </motion.span>
          )}
        </AnimatePresence>

        {/* Voice Assistant — fuellt den restlichen Platz nach rechts */}
        {!sidebarCollapsed && (
          <div className="ml-auto">
            <VoiceButton size={32} variant="sidebar" />
          </div>
        )}
      </div>

      {/* Collapsed-State: kleiner Voice-Button als Zeile (sidebar-collapsed=48px) */}
      {sidebarCollapsed && (
        <div className="flex justify-center py-2" style={{ borderBottom: "1px solid var(--color-p2-line2)" }}>
          <VoiceButton size={32} variant="sidebar" />
        </div>
      )}

      {/* Navigation — gruppiert, Text-first; Icons nur im Collapsed-Modus */}
      <nav className="flex-1 py-2 overflow-y-auto overflow-x-hidden">
        {NAV_GROUPS.map((group) => {
          if (group.items.length === 0) return null;
          return (
            <div key={t(group.labelKey)}>
              {!sidebarCollapsed && (
                <div
                  className="px-4 pt-3 pb-1 select-none first:pt-1"
                  style={{
                    fontFamily: "var(--font-p2-display)",
                    fontWeight: 700,
                    fontSize: "9px",
                    letterSpacing: "0.2em",
                    color: "var(--color-p2-faint)",
                  }}
                >
                  {t(group.labelKey)}
                </div>
              )}
              <ul className={sidebarCollapsed ? "space-y-px px-1" : "space-y-px px-2"}>
                {group.items.map(({ href, icon: Icon, labelKey }) => {
                  const label = t(labelKey);
                  const isActive =
                    href === "/" ? pathname === "/" : pathname.startsWith(href);
                  const showBadge = href === "/inbox" && hasPendingApprovals;

                  return (
                    <li key={href}>
                      <Link
                        href={href}
                        className="group relative flex items-center gap-2 cursor-pointer"
                        style={{
                          minHeight: sidebarCollapsed ? "40px" : "36px",
                          justifyContent: sidebarCollapsed ? "center" : "flex-start",
                          padding: sidebarCollapsed ? 0 : "0 9px",
                          ...MONO,
                          fontSize: sidebarCollapsed ? undefined : "12px",
                          fontWeight: isActive ? 700 : 400,
                          // System A: der Aktivzustand war eine volle Akzent-
                          // Fläche pro Zeile — auf 224px Sidebar-Breite ein
                          // Bone-Block, der die Navigation zum lautesten
                          // Element machte. Jetzt: dunkle Fläche + Gewicht,
                          // der Akzent bleibt als schmaler Marker (unten).
                          backgroundColor: isActive ? "var(--color-p2-pan2)" : "transparent",
                          color: isActive ? "var(--color-p2-txt)" : "var(--color-p2-dim)",
                        }}
                        title={sidebarCollapsed ? label : undefined}
                      >
                        {isActive && (
                          <span
                            aria-hidden
                            className="absolute left-0 top-0 bottom-0 w-[2px]"
                            style={{ backgroundColor: "var(--color-p2-amb)" }}
                          />
                        )}
                        {sidebarCollapsed ? (
                          <span className="relative shrink-0 flex items-center justify-center">
                            <Icon
                              size={17}
                              strokeWidth={isActive ? 2 : 1.75}
                              style={{ color: isActive ? "var(--color-p2-txt)" : "var(--color-p2-dim)" }}
                            />
                            {showBadge && (
                              <span
                                className="absolute -top-1 -right-1 w-2 h-2 rounded-full"
                                style={{
                                  backgroundColor: "var(--color-p2-err)",
                                }}
                              />
                            )}
                          </span>
                        ) : (
                          <>
                            <span className="flex-1 whitespace-nowrap overflow-hidden">{label}</span>
                            {showBadge && (
                              <span
                                className="w-2 h-2 rounded-full shrink-0"
                                style={{
                                  backgroundColor: "var(--color-p2-err)",
                                }}
                              />
                            )}
                          </>
                        )}

                        {/* Tooltip for collapsed state */}
                        {sidebarCollapsed && (
                          <div
                            className="absolute left-full ml-2 px-2 py-1 whitespace-nowrap opacity-0 group-hover:opacity-100 pointer-events-none transition-opacity z-50"
                            style={{
                              ...MONO,
                              fontSize: "11px",
                              backgroundColor: "var(--color-p2-pan2)",
                              border: "1px solid var(--color-p2-line)",
                              color: "var(--color-p2-txt)",
                              boxShadow: "var(--shadow-elevated)",
                            }}
                          >
                            {label}
                          </div>
                        )}
                      </Link>
                    </li>
                  );
                })}
              </ul>
            </div>
          );
        })}
      </nav>

      {/* Bottom: user info + logout */}
      <div
        className="shrink-0"
        style={{ borderTop: "1px solid var(--color-p2-line2)" }}
      >
        {currentUser && !sidebarCollapsed && (
          <div className="px-4 pt-3 pb-1">
            <div
              className="truncate"
              style={{ ...MONO, fontSize: "12px", fontWeight: 700, color: "var(--color-p2-txt)" }}
            >
              {currentUser.name}
            </div>
            <div
              className="truncate"
              style={{ ...MONO, fontSize: "10px", color: "var(--color-p2-dim)" }}
            >
              {currentUser.email}
            </div>
          </div>
        )}

        <button
          onClick={handleLogout}
          title={t("logout")}
          className="flex items-center gap-2 w-full px-4 min-h-touch cursor-pointer"
          style={{
            ...MONO,
            fontSize: "11px",
            letterSpacing: "0.08em",
            color: "var(--color-p2-dim)",
            justifyContent: sidebarCollapsed ? "center" : "flex-start",
          }}
          onMouseEnter={(e) =>
            ((e.currentTarget as HTMLElement).style.color = "var(--color-p2-err)")
          }
          onMouseLeave={(e) =>
            ((e.currentTarget as HTMLElement).style.color = "var(--color-p2-dim)")
          }
        >
          {sidebarCollapsed ? <LogOut size={15} /> : <span>{t("logout").toUpperCase()} →</span>}
        </button>
      </div>
    </motion.aside>
  );
}
