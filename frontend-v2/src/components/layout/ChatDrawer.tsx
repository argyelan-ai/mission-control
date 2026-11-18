"use client";

import Link from "next/link";
import { useTranslations } from "next-intl";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { BookOpen, Gauge, PanelLeft, Settings, type LucideIcon } from "lucide-react";
import { alpha, C } from "@/lib/colors";
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
  const { drawerOpen, setDrawerOpen } = useMobileNav();
  const reduce = useReducedMotion();
  const close = () => setDrawerOpen(false);

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
          <motion.nav
            id="chat-drawer"
            role="dialog"
            aria-modal="true"
            aria-label={t("chatList")}
            data-testid="chat-drawer"
            className="absolute inset-y-0 left-0 flex flex-col w-[85%] max-w-sm pt-island pb-safe-bottom"
            style={{ background: "var(--color-p2-pan)", borderRight: "1px solid var(--color-p2-line2)", boxShadow: "var(--shadow-elevated)" }}
            initial={{ x: "-100%" }}
            animate={{ x: 0 }}
            exit={{ x: "-100%" }}
            transition={{ duration: reduce ? 0 : 0.25, ease: [0.16, 1, 0.3, 1] }}
          >
            <div className="flex-1 min-h-0 overflow-y-auto px-3 pt-3">
              <ChatList onNavigate={close} />
            </div>
            <div className="shrink-0 px-3 pt-2 pb-3 flex flex-col" style={{ borderTop: "1px solid var(--color-p2-line2)" }}>
              <FooterLink href={TAB_HREFS.models} icon={Gauge} label={t("tabModels")} onNavigate={close} />
              <FooterLink href={TAB_HREFS.knowledge} icon={BookOpen} label={t("tabKnowledge")} onNavigate={close} />
              <FooterLink href="/settings" icon={Settings} label={t("settings")} onNavigate={close} />
            </div>
          </motion.nav>
        </div>
      )}
    </AnimatePresence>
  );
}
