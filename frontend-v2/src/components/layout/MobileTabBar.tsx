"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useTranslations } from "next-intl";
import { C } from "@/lib/colors";
import { activeTabFor, chatTabHref, TAB_HREFS, TAB_ICONS, TAB_LABEL_KEYS, TAB_ORDER, type MobileTabKey } from "@/lib/mobileNav";
import { badgeLabel } from "@/lib/inbox";
import { loadRecentChats, RECENT_CHATS_EVENT, type ChatRef } from "@/lib/recentChats";
import { useWaitingCount } from "@/hooks/useWaitingCount";
import { useKeyboardOpen } from "@/hooks/useKeyboardOpen";

/** The last chats of this device, kept fresh when another screen opens one. */
function useRecentChats(): ChatRef[] {
  const [recent, setRecent] = useState<ChatRef[]>([]);
  useEffect(() => {
    const load = () => setRecent(loadRecentChats());
    load();
    window.addEventListener(RECENT_CHATS_EVENT, load);
    window.addEventListener("storage", load);
    return () => {
      window.removeEventListener(RECENT_CHATS_EVENT, load);
      window.removeEventListener("storage", load);
    };
  }, []);
  return recent;
}

export { useRecentChats };

function TabIcon({ tab, active, badge }: { tab: MobileTabKey; active: boolean; badge?: string | null }) {
  const Icon = TAB_ICONS[tab];
  return (
    <span
      className="relative flex items-center justify-center w-14 h-8 rounded-full transition-colors motion-reduce:transition-none"
      style={{ background: active ? C.accentSubtle : "transparent" }}
    >
      <Icon size={20} strokeWidth={active ? 2 : 1.75} aria-hidden />
      {badge && (
        <span
          data-testid="chat-badge"
          aria-hidden
          className="absolute -top-1 right-1 min-w-5 h-5 px-1 rounded-full flex items-center justify-center text-xs tabular-nums"
          style={{ background: C.accent, color: C.onAccent, fontWeight: 600 }}
        >
          {badge}
        </span>
      )}
    </span>
  );
}

/**
 * MobileTabBar — the phone's bottom bar „Drei Daumen" (operator decision
 * 2026-11-18, ADR-102): Chat · Models · Knowledge.
 *
 * - Chat opens the last chat (the sessions page restores it) and carries the
 *   count of what waits on the operator — approvals, head questions and
 *   operator reviews, the same function the Inbox page renders
 *   (lib/inbox.ts). Nothing waits → no badge.
 * - Models is the runtimes page (bench is reached from there), Knowledge the
 *   landing that leads to Memory and Skills.
 * - Everything else left the bar: Settings › More lists it, every route
 *   still works.
 *
 * Shown on the chat screen too, under the composer (variant C). Not
 * `position: fixed`: a flex child of the shell column, so it sits at the
 * bottom of the h-dvh box on iOS too. The bar paints down into the home
 * indicator strip; its targets end above it (`safe-area-inset-bottom`). It
 * steps aside while the keyboard is up. Desktop: hidden (`md:hidden`).
 */
export function MobileTabBar() {
  const t = useTranslations("cockpit");
  const pathname = usePathname() ?? "/";
  // Inbox "For you" plus a red journey run — the same number as the sidebar.
  const count = useWaitingCount();
  const recent = useRecentChats();
  const keyboardOpen = useKeyboardOpen();
  const active = activeTabFor(pathname);
  const badge = badgeLabel(count);

  if (keyboardOpen) return null;

  return (
    <nav
      aria-label={t("mainNav")}
      data-testid="mobile-tab-bar"
      className="md:hidden shrink-0"
      style={{
        backgroundColor: "var(--color-p2-pan)",
        borderTop: "1px solid var(--color-p2-line)",
        paddingBottom: "env(safe-area-inset-bottom)",
      }}
    >
      <div className="grid grid-cols-3">
        {TAB_ORDER.map((key) => {
          const on = active === key;
          const label = t(TAB_LABEL_KEYS[key]);
          const tabBadge = key === "chat" ? badge : null;
          return (
            <Link
              key={key}
              href={key === "chat" ? chatTabHref(recent) : TAB_HREFS[key]}
              aria-current={on ? "page" : undefined}
              aria-label={tabBadge ? `${label}, ${t("chatWaiting", { count })}` : undefined}
              data-tab={key}
              className="flex flex-col items-center justify-center gap-1 min-h-14 pt-2 pb-1 cursor-pointer"
              style={{ color: on ? C.textPrimary : C.textMuted }}
            >
              <TabIcon tab={key} active={on} badge={tabBadge} />
              <span className="text-xs leading-none" style={{ fontWeight: on ? 600 : 500 }}>
                {label}
              </span>
            </Link>
          );
        })}
      </div>
    </nav>
  );
}
