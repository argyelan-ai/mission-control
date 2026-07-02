"use client";

import { useState, useEffect } from "react";
import { usePathname } from "next/navigation";
import Link from "next/link";
import { motion, AnimatePresence } from "framer-motion";
import { Menu, X, Check } from "lucide-react";
import { NAV_ITEMS } from "./Sidebar";
import { cn } from "@/lib/utils";
import { clearToken, api } from "@/lib/api";
import { useRouter } from "next/navigation";
import { LogOut } from "lucide-react";
import { useAppStore } from "@/lib/store";
import { useQuery } from "@tanstack/react-query";
import type { Approval, Board } from "@/lib/types";
import { VoiceButton } from "@/components/voice/VoiceWidget";
import { C } from "@/lib/colors";
import { useBodyScrollLock } from "@/hooks/useBodyScrollLock";

export default function MobileNav() {
  const [open, setOpen] = useState(false);
  const pathname = usePathname();
  const router = useRouter();
  const { currentUser, activeBoardId, setActiveBoardId } = useAppStore();

  const { data: approvals } = useQuery<Approval[]>({
    queryKey: ["approvals-badge"],
    queryFn: () => api.approvals.list(),
    refetchInterval: 30_000,
  });
  const hasPendingApprovals = (approvals ?? []).some((a) => a.status === "pending");

  // Boards — same query key as WorkspaceSwitcher to share cache
  const { data: boardsData } = useQuery<Board[]>({
    queryKey: ["boards"],
    queryFn: api.boards.list,
  });
  const boards = boardsData ?? [];
  const activeBoard = boards.find((b) => b.id === activeBoardId) ?? boards[0] ?? null;
  const hasMultipleBoards = boards.length > 1;

  // Close on route change
  useEffect(() => {
    setOpen(false);
  }, [pathname]);

  // Prevent body scroll when menu open — iOS-fest via Fixed-Position-Technik (MOBILE-SPEC M4)
  useBodyScrollLock(open);

  function handleLogout() {
    clearToken();
    setOpen(false);
    router.replace("/login");
  }

  function handleBoardSelect(id: string) {
    setActiveBoardId(id);
    setOpen(false);
  }

  return (
    <>
      {/* Top bar — pt-island keeps content below Dynamic Island on iPhone 14 Pro+.
          <header> = Banner-Landmark (a11y); opak statt backdrop-blur — blur auf
          position:fixed erzeugt auf iOS Scroll-Jank (MOBILE-SPEC M8 + Flach-Regel). */}
      <header
        className="fixed top-0 left-0 right-0 z-40 flex items-end justify-between px-4 md:hidden pt-island"
        style={{
          paddingBottom: "0.5rem",
          minHeight: "calc(env(safe-area-inset-top) + 3.5rem)",
          backgroundColor: "rgba(5, 5, 6, 0.97)",
          borderBottom: "1px solid var(--color-border-subtle)",
        }}
      >
        {/* Hamburger — left, well below the island */}
        <button
          onClick={() => setOpen(!open)}
          className="flex items-center justify-center w-11 h-11 rounded-lg cursor-pointer"
          style={{ color: "var(--color-text-secondary)" }}
          aria-label={open ? "Menü schliessen" : "Menü öffnen"}
        >
          {open ? <X size={20} /> : <Menu size={20} />}
        </button>

        {/* Voice-Assistant — Pendant zum Hamburger links */}
        <VoiceButton size={40} variant="header" />
      </header>

      {/* Overlay + slide-out menu */}
      <AnimatePresence>
        {open && (
          <>
            {/* Backdrop */}
            <motion.div
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              transition={{ duration: 0.2 }}
              className="fixed inset-0 z-40 md:hidden"
              style={{ backgroundColor: "rgba(0, 0, 0, 0.6)" }}
              onClick={() => setOpen(false)}
            />

            {/* Menu panel — slides from RIGHT to avoid Safari Edge-Back-Swipe (MOBILE-SPEC M7).
                top-0 + pt-safe statt top-14 damit safe-area korrekt behandelt wird. */}
            <motion.div
              initial={{ x: "100%" }}
              animate={{ x: 0 }}
              exit={{ x: "100%" }}
              transition={{ duration: 0.3, ease: [0.16, 1, 0.3, 1] }}
              className="fixed top-0 right-0 bottom-0 z-50 w-72 flex flex-col md:hidden pt-safe"
              style={{
                backgroundColor: "rgba(5, 5, 6, 0.98)",
                borderLeft: "1px solid var(--color-border-subtle)",
                boxShadow: "0 4px 24px rgba(0,0,0,0.5), 0 1px 2px rgba(0,0,0,0.3)",
              }}
            >
              <nav className="flex-1 py-4 overflow-y-auto">
                <ul className="space-y-1 px-3">
                  {NAV_ITEMS.map(({ href, icon: Icon, label }) => {
                    const isActive =
                      href === "/"
                        ? pathname === "/"
                        : pathname.startsWith(href);
                    const showBadge = href === "/inbox" && hasPendingApprovals;

                    return (
                      <li key={href}>
                        <Link
                          href={href}
                          className={cn(
                            "relative flex items-center gap-3 px-3 py-3 rounded-lg text-sm transition-all min-h-[44px]",
                            isActive
                              ? "text-[var(--color-accent-light)]"
                              : "text-[var(--color-text-secondary)]"
                          )}
                          style={{
                            backgroundColor: isActive
                              ? "var(--color-accent-subtle)"
                              : "transparent",
                          }}
                        >
                          {isActive && (
                            <div
                              className="absolute left-0 top-1/2 -translate-y-1/2 w-[2px] h-5 rounded-full"
                              style={{
                                backgroundColor: "var(--color-accent)",
                              }}
                            />
                          )}
                          <div className="relative shrink-0">
                            <Icon size={18} />
                            {showBadge && (
                              <span
                                className="absolute -top-1 -right-1 w-2 h-2 rounded-full"
                                style={{
                                  backgroundColor: "var(--color-error)",
                                }}
                              />
                            )}
                          </div>
                          <span style={{ fontWeight: isActive ? 500 : 400 }}>
                            {label}
                          </span>
                        </Link>
                      </li>
                    );
                  })}
                </ul>
              </nav>

              {/* Bottom section: board switcher → user info + logout */}
              <div
                className="px-3 py-3"
                style={{
                  borderTop: "1px solid var(--color-border-subtle)",
                }}
              >
                {/* Board switcher — shown above user info */}
                {boards.length > 0 && (
                  <div className="mb-1">
                    {/* Section label */}
                    <div
                      className="px-3 pb-1 text-xs font-medium uppercase tracking-widest"
                      style={{ color: C.textDim, letterSpacing: "0.08em" }}
                    >
                      Board
                    </div>

                    {hasMultipleBoards ? (
                      // Board list — tappable rows
                      <ul>
                        {boards.map((board) => {
                          const isActive = board.id === activeBoardId || board.id === activeBoard?.id;
                          const boardColor = board.color ?? C.accent;
                          return (
                            <li key={board.id}>
                              <button
                                onClick={() => handleBoardSelect(board.id)}
                                className="w-full flex items-center gap-2.5 px-3 text-sm rounded-lg transition-colors cursor-pointer text-left"
                                style={{
                                  minHeight: "44px",
                                  backgroundColor: isActive ? C.accentSubtle : "transparent",
                                  color: isActive ? C.accent : "var(--color-text-secondary)",
                                }}
                                onMouseEnter={(e) => {
                                  if (!isActive) (e.currentTarget as HTMLButtonElement).style.backgroundColor = C.bgHover;
                                }}
                                onMouseLeave={(e) => {
                                  if (!isActive) (e.currentTarget as HTMLButtonElement).style.backgroundColor = "transparent";
                                }}
                              >
                                {board.icon ? (
                                  <span className="shrink-0 text-base leading-none w-5 text-center">{board.icon}</span>
                                ) : (
                                  <span
                                    className="shrink-0 w-3 h-3 rounded-full"
                                    style={{ backgroundColor: boardColor }}
                                  />
                                )}
                                <span className="flex-1 truncate" style={{ fontWeight: isActive ? 500 : 400 }}>
                                  {board.name}
                                </span>
                                {isActive && (
                                  <Check size={13} className="shrink-0" style={{ color: C.accent }} />
                                )}
                              </button>
                            </li>
                          );
                        })}
                      </ul>
                    ) : (
                      // Single board — static row, no interaction needed
                      <div
                        className="flex items-center gap-2.5 px-3"
                        style={{ minHeight: "44px" }}
                      >
                        {activeBoard?.icon ? (
                          <span className="shrink-0 text-base leading-none w-5 text-center">{activeBoard.icon}</span>
                        ) : (
                          <span
                            className="shrink-0 w-3 h-3 rounded-full"
                            style={{ backgroundColor: activeBoard?.color ?? C.accent }}
                          />
                        )}
                        <span
                          className="flex-1 truncate text-sm font-medium"
                          style={{ color: "var(--color-text-secondary)" }}
                        >
                          {activeBoard?.name ?? "Board"}
                        </span>
                      </div>
                    )}
                  </div>
                )}

                {/* User info + logout */}
                <div
                  className="pt-2"
                  style={{ borderTop: boards.length > 0 ? `1px solid ${C.borderSubtle}` : "none" }}
                >
                  {currentUser && (
                    <div className="px-3 pb-2">
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
                    className="flex items-center gap-3 w-full px-3 py-2 text-sm rounded-lg cursor-pointer"
                    style={{ color: "var(--color-text-muted)" }}
                  >
                    <LogOut size={16} />
                    <span>Logout</span>
                  </button>
                </div>
              </div>
            </motion.div>
          </>
        )}
      </AnimatePresence>
    </>
  );
}
