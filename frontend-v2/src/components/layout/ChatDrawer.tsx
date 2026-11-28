"use client";

import { useEffect, useRef } from "react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { BookOpen, Gauge, PanelLeft, Search, Settings, type LucideIcon } from "lucide-react";
import { alpha, C } from "@/lib/colors";
import { useAppStore } from "@/lib/store";
import { TAB_HREFS } from "@/lib/mobileNav";
import { ChatList } from "./ChatList";
import { useMobileNav, useMobileNavOptional } from "./MobileNav";

/** The sidebar icon that opens the drawer — chat header and app bar alike.
 *  Visible circle 36 px, finger target 44 px (K11). */
export function DrawerButton({ className = "" }: { className?: string }) {
  const t = useTranslations("cockpit");
  const nav = useMobileNavOptional();
  if (!nav) return null;
  const { drawerOpen, setDrawerOpen } = nav;
  return (
    <button
      type="button"
      onClick={() => setDrawerOpen(true)}
      aria-label={t("openChats")}
      aria-expanded={drawerOpen}
      aria-controls="chat-drawer"
      data-testid="drawer-button"
      className={`flex items-center justify-center w-11 h-11 shrink-0 cursor-pointer ${className}`}
      style={{ color: C.textSecondary }}
    >
      <PanelLeft size={20} strokeWidth={1.75} aria-hidden />
    </button>
  );
}

function FooterLink({ href, icon: Icon, label, onNavigate }: { href: string; icon: LucideIcon; label: string; onNavigate: () => void }) {
  return (
    <Link
      href={href}
      onClick={onNavigate}
      className="flex items-center gap-3 min-h-11 px-3 rounded-lg text-sm transition-colors hover:bg-[var(--color-bg-hover)]"
      style={{ color: C.textSecondary }}
    >
      <Icon size={18} strokeWidth={1.75} aria-hidden />
      {label}
    </Link>
  );
}

/**
 * ChatDrawer — the phone's chat menu (variant A, operator decision
 * 2026-11-18): slides in from the left over the chat. "New chat", the chats
 * by section, and at the bottom Models · Knowledge · Settings. Closes on a
 * pick, on the scrim, on Esc and on any route change (MobileNavProvider).
 * Desktop never shows it — the same list stands in the left column there.
 */
export function ChatDrawer() {
  const t = useTranslations("cockpit");
  const tShell = useTranslations("shell");
  const setCommandPaletteOpen = useAppStore((s) => s.setCommandPaletteOpen);
  const { drawerOpen, setDrawerOpen } = useMobileNav();
  const reduce = useReducedMotion();
  const close = () => setDrawerOpen(false);
  const panelRef = useRef<HTMLDivElement>(null);

  // Focus (shell review D1): into the drawer on open, kept inside while it is
  // open (Tab cycles), back to whatever opened it on close — a modal dialog
  // must not let Tab wander into the chat behind the scrim.
  useEffect(() => {
    if (!drawerOpen) return;
    const opener = document.activeElement as HTMLElement | null;
    const focusables = () =>
      [...(panelRef.current?.querySelectorAll<HTMLElement>("a[href], button:not([disabled])") ?? [])];
    // The dialog itself takes focus (not "New chat": a ring around the first
    // button read as a selection in the operator's picture); Tab goes on
    // from there into the list.
    panelRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Tab") return;
      const list = focusables();
      if (list.length === 0) return;
      const first = list[0];
      const last = list[list.length - 1];
      if (document.activeElement === panelRef.current) {
        e.preventDefault();
        (e.shiftKey ? last : first).focus();
      } else if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      opener?.focus?.();
    };
  }, [drawerOpen]);

  return (
    <AnimatePresence>
      {drawerOpen && (
        <div className="md:hidden fixed inset-0 z-50" data-testid="chat-drawer-layer">
          <motion.button
            type="button"
            aria-label={t("closeChats")}
            onClick={close}
            className="absolute inset-0 cursor-pointer"
            style={{ background: alpha(C.scrim, 0.75) }}
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: reduce ? 0 : 0.2 }}
          />
          <motion.div
            ref={panelRef}
            id="chat-drawer"
            tabIndex={-1}
            role="dialog"
            aria-modal="true"
            aria-label={t("chatList")}
            data-testid="chat-drawer"
            className="absolute inset-y-0 left-0 flex flex-col w-[85%] max-w-sm pt-island pb-safe-bottom"
            // The panel holds focus only as a starting point for Tab — no ring around
            // the whole drawer (the global focus-visible rule would draw one).
            style={{ background: "var(--color-p2-pan)", borderRight: "1px solid var(--color-p2-line2)", boxShadow: "var(--shadow-elevated)", outline: "none" }}
            initial={{ x: "-100%" }}
            animate={{ x: 0 }}
            exit={{ x: "-100%" }}
            transition={{ duration: reduce ? 0 : 0.25, ease: [0.16, 1, 0.3, 1] }}
          >
            {/* The magnifier sits on the "New chat" row, right of it (concept
                "Suche" 5.2): the list's first row leaves it 48 px. It opens
                the search full screen; the drawer closes underneath. After the
                list in the DOM, so Tab reaches "New chat" first. */}
            <div className="relative flex-1 min-h-0 overflow-y-auto px-3 pt-3 [&_[data-testid=chat-list-new]]:mr-12">
              <ChatList onNavigate={close} />
              <button
                type="button"
                onClick={() => {
                  close();
                  setCommandPaletteOpen(true);
                }}
                aria-label={tShell("search")}
                data-testid="drawer-search"
                className="absolute top-3 right-3 flex items-center justify-center w-11 h-11 rounded-lg cursor-pointer transition-colors hover:bg-[var(--color-bg-hover)]"
                style={{ color: C.textSecondary }}
              >
                <Search size={18} strokeWidth={1.75} aria-hidden />
              </button>
            </div>
            <nav
              aria-label={t("mainNav")}
              className="shrink-0 px-3 pt-2 pb-3 flex flex-col"
              style={{ borderTop: "1px solid var(--color-p2-line2)" }}
            >
              <FooterLink href={TAB_HREFS.models} icon={Gauge} label={t("tabModels")} onNavigate={close} />
              <FooterLink href={TAB_HREFS.knowledge} icon={BookOpen} label={t("tabKnowledge")} onNavigate={close} />
              <FooterLink href="/settings" icon={Settings} label={t("settings")} onNavigate={close} />
            </nav>
          </motion.div>
        </div>
      )}
    </AnimatePresence>
  );
}
