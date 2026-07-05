"use client";

import { usePathname, useRouter } from "next/navigation";
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
} from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { useAppStore } from "@/lib/store";
import { clearToken, api } from "@/lib/api";
import { cn } from "@/lib/utils";
import type { Approval } from "@/lib/types";
import { VoiceButton } from "@/components/voice/VoiceWidget";
import { C } from "@/lib/colors";
import { VERTICALS } from "@/lib/verticals";

// Wordmark: env-getrieben — Deployments branden via NEXT_PUBLIC_BRAND
// ("main.accent"-Split am letzten Punkt; Default = Produktname).
const _BRAND = process.env.NEXT_PUBLIC_BRAND || "Mission.Control";
const _dot = _BRAND.lastIndexOf(".");
const BRAND_MAIN = _dot > 0 ? _BRAND.slice(0, _dot) : _BRAND;
const BRAND_ACCENT = _dot > 0 ? _BRAND.slice(_dot) : "";

export const NAV_ITEMS = [
  { href: "/", icon: Home, label: "Home" },
  { href: "/tasks", icon: FolderKanban, label: "Tasks" },
  { href: "/agents", icon: Bot, label: "Agents" },
  { href: "/office", icon: Building2, label: "Office" },
  { href: "/inbox", icon: Inbox, label: "Inbox" },
  { href: "/insights", icon: TrendingUp, label: "Insights" },
  { href: "/memory", icon: Brain, label: "Memory" },
  { href: "/files", icon: FolderOpen, label: "Files" },
  // News-Studio vertical — stripped from the public-release build
  ...(VERTICALS.newsStudio
    ? [
        { href: "/content", icon: PenLine, label: "Content" },
        { href: "/news", icon: Newspaper, label: "News" },
      ]
    : []),
  { href: "/repos", icon: FolderGit2, label: "Repos" },
  { href: "/skills", icon: Puzzle, label: "Skills" },
  { href: "/runtimes", icon: Server, label: "Runtimes" },
  { href: "/sessions", icon: Terminal, label: "Sessions" },
  { href: "/loops", icon: Repeat, label: "Loops" },
  { href: "/schedule", icon: Calendar, label: "Schedule" },
  { href: "/settings", icon: Settings, label: "Settings" },
];

export default function Sidebar() {
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
        backgroundColor: "rgba(255, 255, 255, 0.02)",
        backdropFilter: "blur(16px)",
        WebkitBackdropFilter: "blur(16px)",
        borderRight: "1px solid var(--color-border-subtle)",
      }}
    >
      {/* Logo area */}
      <div
        className="shrink-0 flex items-center gap-3 px-3 h-14"
        style={{ borderBottom: "1px solid var(--color-border-subtle)" }}
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
                color: "var(--color-text-primary)",
                fontFamily: "var(--font-wordmark), ui-sans-serif, system-ui",
                fontWeight: 500,
                fontSize: "17px",
                letterSpacing: "-0.03em",
              }}
            >
              a<span style={{ color: C.accent }}>.</span>
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
                color: "var(--color-text-primary)",
                fontFamily: "var(--font-wordmark), ui-sans-serif, system-ui",
                fontWeight: 500,
                fontSize: "17px",
                letterSpacing: "-0.03em",
              }}
            >
              {BRAND_MAIN}
              <span style={{ color: C.accent }}>{BRAND_ACCENT}</span>
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
        <div className="flex justify-center py-2" style={{ borderBottom: "1px solid var(--color-border-subtle)" }}>
          <VoiceButton size={32} variant="sidebar" />
        </div>
      )}

      {/* Navigation */}
      <nav className="flex-1 py-3 overflow-y-auto overflow-x-hidden">
        <ul className="space-y-0.5 px-2">
          {NAV_ITEMS.map(({ href, icon: Icon, label }) => {
            const isActive =
              href === "/" ? pathname === "/" : pathname.startsWith(href);
            const showBadge = href === "/inbox" && hasPendingApprovals;

            return (
              <li key={href}>
                <Link
                  href={href}
                  className={cn(
                    "group relative flex items-center gap-3 px-2 py-2 min-h-[44px] rounded-lg transition-all text-sm cursor-pointer",
                    isActive
                      ? "text-[var(--color-accent-light)]"
                      : "text-[var(--color-text-secondary)] hover:text-[var(--color-text-primary)]"
                  )}
                  style={{
                    backgroundColor: isActive
                      ? "var(--color-accent-subtle)"
                      : "transparent",
                  }}
                  title={sidebarCollapsed ? label : undefined}
                >
                  {/* Active indicator bar */}
                  {isActive && (
                    <motion.div
                      layoutId="sidebar-active"
                      className="absolute left-0 top-1/2 -translate-y-1/2 w-[2px] h-5 rounded-full"
                      style={{
                        backgroundColor: "var(--color-accent)",
                        boxShadow: "0 0 8px rgba(15, 163, 163, 0.45)",
                      }}
                      transition={{ type: "spring", stiffness: 380, damping: 30 }}
                    />
                  )}

                  <div className="relative shrink-0">
                    <Icon size={18} />
                    {showBadge && (
                      <span
                        className="absolute -top-1 -right-1 w-2 h-2 rounded-full"
                        style={{
                          backgroundColor: "var(--color-error)",
                          boxShadow: "0 0 6px rgba(194, 56, 56, 0.5)",
                        }}
                      />
                    )}
                  </div>

                  <AnimatePresence>
                    {!sidebarCollapsed && (
                      <motion.span
                        initial={{ opacity: 0, width: 0 }}
                        animate={{ opacity: 1, width: "auto" }}
                        exit={{ opacity: 0, width: 0 }}
                        transition={{ duration: 0.15 }}
                        className="whitespace-nowrap overflow-hidden"
                        style={{ fontWeight: isActive ? 500 : 400 }}
                      >
                        {label}
                      </motion.span>
                    )}
                  </AnimatePresence>

                  {/* Tooltip for collapsed state */}
                  {sidebarCollapsed && (
                    <div
                      className="absolute left-full ml-2 px-2 py-1 rounded-md text-xs whitespace-nowrap opacity-0 group-hover:opacity-100 pointer-events-none transition-opacity z-50"
                      style={{
                        backgroundColor: "var(--color-bg-elevated)",
                        border: "1px solid var(--color-border)",
                        color: "var(--color-text-primary)",
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
      </nav>

      {/* Bottom: user info + logout */}
      <div
        className="shrink-0"
        style={{ borderTop: "1px solid var(--color-border-subtle)" }}
      >
        {currentUser && !sidebarCollapsed && (
          <div className="px-3 pt-3 pb-1">
            <div
              className="text-sm font-medium truncate"
              style={{ color: "var(--color-text-primary)" }}
            >
              {currentUser.name}
            </div>
            <div
              className="text-xs truncate"
              style={{ color: "var(--color-text-muted)" }}
            >
              {currentUser.email}
            </div>
          </div>
        )}

        <button
          onClick={handleLogout}
          title="Logout"
          className="group flex items-center gap-3 w-full px-3 py-2.5 min-h-touch text-sm transition-colors cursor-pointer"
          style={{ color: "var(--color-text-muted)" }}
          onMouseEnter={(e) =>
            ((e.currentTarget as HTMLElement).style.color =
              "var(--color-error)")
          }
          onMouseLeave={(e) =>
            ((e.currentTarget as HTMLElement).style.color =
              "var(--color-text-muted)")
          }
        >
          <LogOut size={16} className="shrink-0" />
          <AnimatePresence>
            {!sidebarCollapsed && (
              <motion.span
                initial={{ opacity: 0, width: 0 }}
                animate={{ opacity: 1, width: "auto" }}
                exit={{ opacity: 0, width: 0 }}
                transition={{ duration: 0.15 }}
                className="whitespace-nowrap overflow-hidden"
              >
                Logout
              </motion.span>
            )}
          </AnimatePresence>
        </button>
      </div>
    </motion.aside>
  );
}
