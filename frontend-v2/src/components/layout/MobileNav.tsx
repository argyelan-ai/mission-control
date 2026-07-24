"use client";

import { useState, useEffect } from "react";
import { usePathname } from "next/navigation";
import Link from "next/link";
import { motion, AnimatePresence } from "framer-motion";
import { NAV_GROUPS } from "./Sidebar";
import { channelFor } from "./channel";
import { clearToken, api } from "@/lib/api";
import { useRouter } from "next/navigation";
import { useAppStore } from "@/lib/store";
import { useQuery } from "@tanstack/react-query";
import type { Approval, Board } from "@/lib/types";
import { VoiceButton } from "@/components/voice/VoiceWidget";
import { P2 } from "@/lib/colors";
import { EntityIcon } from "@/components/shared/EntityIcon";
import { useBodyScrollLock } from "@/hooks/useBodyScrollLock";

// Wordmark (gleiche Logik wie Sidebar — env-getrieben)
const _BRAND = process.env.NEXT_PUBLIC_BRAND || "Mission.Control";
const _dot = _BRAND.lastIndexOf(".");
const BRAND_MAIN = _dot > 0 ? _BRAND.slice(0, _dot) : _BRAND;
const BRAND_ACCENT = _dot > 0 ? _BRAND.slice(_dot) : "";

// P2: Bottom-Tab-Bar — 4 Kernziele + Index. Text + Kanal-Nummer, keine Icons.
const TAB_ITEMS = [
  { href: "/", label: "HOME", num: "01" },
  { href: "/tasks", label: "TASKS", num: "02" },
  { href: "/agents", label: "AGTS", num: "03" },
  { href: "/sessions", label: "SESS", num: "04" },
] as const;

const MONO = { fontFamily: "var(--font-p2-mono)" };

export default function MobileNav() {
  const [open, setOpen] = useState(false);
  const pathname = usePathname();
  const router = useRouter();
  const { currentUser, activeBoardId, setActiveBoardId } = useAppStore();
  const { ch } = channelFor(pathname);

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

  function isTabActive(href: string) {
    return href === "/" ? pathname === "/" : pathname.startsWith(href);
  }

  // Ist das aktive Ziel nur über den Index erreichbar? → Index-Tab als aktiv markieren
  const menuCoversCurrent = !TAB_ITEMS.some((t) => isTabActive(t.href));
  const indexActive = open || menuCoversCurrent;

  return (
    <>
      {/* Top bar — Wordmark links, CH-Kennung + Voice rechts. pt-island hält
          Inhalt unter der Dynamic Island; opak statt backdrop-blur (kein iOS Jank). */}
      <header
        className="fixed top-0 left-0 right-0 z-40 flex items-end justify-between px-4 md:hidden pt-island"
        style={{
          paddingBottom: "0.5rem",
          minHeight: "calc(env(safe-area-inset-top) + 3.5rem)",
          backgroundColor: "rgba(8,7,5,0.92)",
          borderBottom: "1px solid var(--color-p2-line2)",
        }}
      >
        <Link
          href="/"
          className="flex items-center h-11 cursor-pointer"
          aria-label="Home"
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
        </Link>

        <div className="flex items-center gap-2">
          <span
            aria-hidden
            style={{
              ...MONO,
              color: "var(--color-p2-amb)",
              border: "1px solid var(--color-p2-amb-d)",
              padding: "3px 7px",
              fontSize: "10px",
              fontWeight: 700,
              letterSpacing: "0.08em",
            }}
          >
            {ch}
          </span>
          <VoiceButton size={40} variant="header" />
        </div>
      </header>

      {/* Bottom tab bar — Daumen-Zone, safe-area-aware. Aktiv = Reverse-Video. */}
      <nav
        aria-label="Hauptnavigation"
        className="fixed bottom-0 left-0 right-0 z-40 md:hidden"
        style={{
          backgroundColor: "rgba(8,7,5,0.95)",
          borderTop: "1px solid var(--color-p2-line)",
          paddingBottom: "env(safe-area-inset-bottom)",
        }}
      >
        <div className="grid grid-cols-5">
          {TAB_ITEMS.map(({ href, label, num }) => {
            const active = isTabActive(href);
            return (
              <Link
                key={href}
                href={href}
                className="flex flex-col items-center justify-center min-h-[52px] cursor-pointer"
                style={{
                  backgroundColor: active ? "var(--color-p2-amb)" : "transparent",
                  color: active ? "var(--color-p2-inv)" : "var(--color-p2-dim)",
                  ...MONO,
                }}
                aria-current={active ? "page" : undefined}
              >
                <span
                  style={{
                    fontStyle: "normal",
                    fontSize: "8px",
                    lineHeight: 1.4,
                    color: active ? "var(--color-p2-inv)" : "var(--color-p2-faint)",
                  }}
                >
                  {num}
                </span>
                <span style={{ fontSize: "10.5px", fontWeight: 700, letterSpacing: "0.06em" }}>
                  {label}
                </span>
              </Link>
            );
          })}

          {/* Index-Tab öffnet den Drawer */}
          <button
            onClick={() => setOpen(true)}
            aria-label="Open menu"
            className="relative flex flex-col items-center justify-center min-h-[52px] cursor-pointer"
            style={{
              backgroundColor: indexActive ? "var(--color-p2-amb)" : "transparent",
              color: indexActive ? "var(--color-p2-inv)" : "var(--color-p2-dim)",
              ...MONO,
            }}
          >
            <span
              style={{
                fontStyle: "normal",
                fontSize: "8px",
                lineHeight: 1.4,
                color: indexActive ? "var(--color-p2-inv)" : "var(--color-p2-faint)",
              }}
            >
              05
            </span>
            <span style={{ fontSize: "10.5px", fontWeight: 700, letterSpacing: "0.06em" }}>
              ≡ INDEX
            </span>
            {hasPendingApprovals && (
              <span
                className="absolute top-1.5 right-1.5 w-2 h-2 rounded-full"
                style={{ backgroundColor: "var(--color-p2-err)" }}
              />
            )}
          </button>
        </div>
      </nav>

      {/* Overlay + slide-out index drawer */}
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
              style={{ backgroundColor: "rgba(5,4,3,0.75)" }}
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
                backgroundColor: "var(--color-p2-pan)",
                borderLeft: "1px solid var(--color-p2-line)",
                boxShadow: "0 4px 24px rgba(0,0,0,0.5), 0 1px 2px rgba(0,0,0,0.3)",
              }}
            >
              {/* Drawer-Header: Wordmark + Close-Key */}
              <div
                className="flex items-center justify-between px-3 h-14 shrink-0"
                style={{ borderBottom: "1px solid var(--color-p2-line2)" }}
              >
                <span
                  style={{
                    color: "var(--color-p2-txt)",
                    fontFamily: "var(--font-p2-display)",
                    fontWeight: 700,
                    fontSize: "14px",
                    letterSpacing: "0.02em",
                  }}
                >
                  {BRAND_MAIN}
                  <span style={{ color: P2.amb }}>{BRAND_ACCENT}</span>
                </span>
                <button
                  onClick={() => setOpen(false)}
                  className="flex items-center justify-center w-11 h-11 cursor-pointer"
                  style={{
                    color: "var(--color-p2-txt)",
                    border: "1px solid var(--color-p2-line)",
                    ...MONO,
                    fontWeight: 700,
                    fontSize: "13px",
                  }}
                  aria-label="Close menu"
                >
                  ✕
                </button>
              </div>

              <nav className="flex-1 py-3 overflow-y-auto">
                {NAV_GROUPS.map((group) => {
                  const items = group.items;
                  if (items.length === 0) return null;
                  return (
                    <div key={group.label} className="px-3">
                      <div
                        className="px-2 pt-3 pb-1 select-none"
                        style={{
                          fontFamily: "var(--font-p2-display)",
                          fontWeight: 700,
                          fontSize: "9px",
                          letterSpacing: "0.2em",
                          color: "var(--color-p2-faint)",
                        }}
                      >
                        {group.label}
                      </div>
                      <ul>
                        {items.map(({ href, label }) => {
                          const isActive = isTabActive(href);
                          const showBadge = href === "/inbox" && hasPendingApprovals;
                          return (
                            <li key={href}>
                              <Link
                                href={href}
                                className="flex items-center gap-2 px-2 cursor-pointer"
                                style={{
                                  minHeight: "44px",
                                  ...MONO,
                                  fontSize: "12.5px",
                                  fontWeight: isActive ? 700 : 400,
                                  backgroundColor: isActive ? "var(--color-p2-amb)" : "transparent",
                                  color: isActive ? "var(--color-p2-inv)" : "var(--color-p2-txt)",
                                }}
                              >
                                <span className="flex-1">{label}</span>
                                {showBadge && (
                                  <span
                                    className="w-2 h-2 rounded-full shrink-0"
                                    style={{
                                      backgroundColor: isActive
                                        ? "var(--color-p2-inv)"
                                        : "var(--color-p2-err)",
                                    }}
                                  />
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

              {/* Bottom section: board switcher → user info + logout */}
              <div
                className="px-3 py-3"
                style={{
                  borderTop: "1px solid var(--color-p2-line2)",
                  paddingBottom: "calc(env(safe-area-inset-bottom) + 0.75rem)",
                }}
              >
                {boards.length > 0 && (
                  <div className="mb-1">
                    <div
                      className="px-2 pb-1"
                      style={{
                        fontFamily: "var(--font-p2-display)",
                        fontWeight: 700,
                        fontSize: "9px",
                        letterSpacing: "0.2em",
                        color: "var(--color-p2-faint)",
                      }}
                    >
                      BOARD
                    </div>

                    {hasMultipleBoards ? (
                      <ul>
                        {boards.map((board) => {
                          const isActive = board.id === activeBoardId || board.id === activeBoard?.id;
                          return (
                            <li key={board.id}>
                              <button
                                onClick={() => handleBoardSelect(board.id)}
                                className="w-full flex items-center gap-2.5 px-2 cursor-pointer text-left"
                                style={{
                                  minHeight: "44px",
                                  ...MONO,
                                  fontSize: "12px",
                                  fontWeight: isActive ? 700 : 400,
                                  backgroundColor: isActive ? "var(--color-p2-amb)" : "transparent",
                                  color: isActive ? "var(--color-p2-inv)" : "var(--color-p2-txt)",
                                }}
                              >
                                {board.icon ? (
                                  <span className="shrink-0 leading-none w-5 text-center">
                                    <EntityIcon value={board.icon} size={14} />
                                  </span>
                                ) : (
                                  <span
                                    className="shrink-0 w-2.5 h-2.5 rounded-full"
                                    style={{
                                      backgroundColor: isActive ? "var(--color-p2-inv)" : (board.color ?? P2.amb),
                                    }}
                                  />
                                )}
                                <span className="flex-1 truncate">{board.name}</span>
                                {isActive && <span className="shrink-0">✓</span>}
                              </button>
                            </li>
                          );
                        })}
                      </ul>
                    ) : (
                      <div className="flex items-center gap-2.5 px-2" style={{ minHeight: "44px" }}>
                        {activeBoard?.icon ? (
                          <span className="shrink-0 leading-none w-5 text-center">
                            <EntityIcon value={activeBoard.icon} size={14} />
                          </span>
                        ) : (
                          <span
                            className="shrink-0 w-2.5 h-2.5 rounded-full"
                            style={{ backgroundColor: activeBoard?.color ?? P2.amb }}
                          />
                        )}
                        <span
                          className="flex-1 truncate"
                          style={{ ...MONO, fontSize: "12px", color: "var(--color-p2-dim)" }}
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
                  style={{ borderTop: boards.length > 0 ? "1px solid var(--color-p2-line2)" : "none" }}
                >
                  {currentUser && (
                    <div className="px-2 pb-2">
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
                    className="flex items-center w-full px-2 cursor-pointer"
                    style={{
                      minHeight: "44px",
                      ...MONO,
                      fontSize: "12px",
                      letterSpacing: "0.08em",
                      color: "var(--color-p2-dim)",
                    }}
                  >
                    LOGOUT →
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
