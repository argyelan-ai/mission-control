"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useTranslations } from "next-intl";
import { Plus } from "lucide-react";
import { C } from "@/lib/colors";
import { activeTabFor, TAB_HREFS, TAB_ICONS, TAB_LABEL_KEYS, type MobileTabKey } from "@/lib/mobileNav";
import { badgeLabel } from "@/lib/inbox";
import { chatsTabHref, loadRecentChats, RECENT_CHATS_EVENT, type ChatRef } from "@/lib/recentChats";
import { useInbox } from "@/hooks/useInbox";
import { useKeyboardOpen } from "@/hooks/useKeyboardOpen";
import { useMobileNav } from "./MobileNav";

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
          data-testid="inbox-badge"
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
 * MobileTabBar — the phone's bottom bar (mobile nav V2 „Schnell-Knopf"):
 * Home · Tasks · ⊕ New · Chats · Inbox.
 *
 * - Inbox carries the real count of what waits on the operator — the same
 *   function the Inbox page renders (lib/inbox.ts), so badge = rows.
 * - Chats opens the last chat directly; on the chats page it leads back to
 *   the list (lib/recentChats.ts).
 * - ⊕ opens the menu sheet (QuickSheet.tsx, variant B „Zwei Ebenen"); it
 *   replaces a "More" tab — every other page sits one level deeper there.
 *
 * Not `position: fixed`: a flex child of the shell column, so it sits at the
 * bottom of the h-dvh box on iOS too. The bar paints down into the home
 * indicator strip; its targets end above it (`safe-area-inset-bottom`). It
 * steps aside while the keyboard is up. Desktop: hidden (`md:hidden`).
 */
export function MobileTabBar() {
  const t = useTranslations("nav");
  const pathname = usePathname() ?? "/";
  const { quickOpen, setQuickOpen } = useMobileNav();
  const { count } = useInbox();
  const recent = useRecentChats();
  const keyboardOpen = useKeyboardOpen();
  const active = activeTabFor(pathname);
  const badge = badgeLabel(count);

  if (keyboardOpen) return null;

  const tab = (key: MobileTabKey, href: string) => {
    const on = active === key;
    const label = t(TAB_LABEL_KEYS[key]);
    return (
      <Link
        key={key}
        href={href}
        aria-current={on ? "page" : undefined}
        aria-label={key === "inbox" && badge ? `${label}, ${t("inboxWaiting", { count })}` : undefined}
        data-tab={key}
        className="flex flex-col items-center justify-center gap-1 min-h-14 pt-2 pb-1 cursor-pointer"
        style={{ color: on ? C.textPrimary : C.textMuted }}
      >
        <TabIcon tab={key} active={on} badge={key === "inbox" ? badge : null} />
        <span className="text-xs leading-none" style={{ fontWeight: on ? 600 : 500 }}>
          {label}
        </span>
      </Link>
    );
  };

  return (
    <nav
      aria-label={t("mobileNav")}
      data-testid="mobile-tab-bar"
      className="md:hidden shrink-0"
      style={{
        backgroundColor: "var(--color-p2-pan)",
        borderTop: "1px solid var(--color-p2-line)",
        paddingBottom: "env(safe-area-inset-bottom)",
      }}
    >
      <div className="grid grid-cols-5">
        {tab("home", TAB_HREFS.home)}
        {tab("tasks", TAB_HREFS.tasks)}
        <button
          type="button"
          onClick={() => setQuickOpen(true)}
          aria-haspopup="dialog"
          aria-expanded={quickOpen}
          data-tab="new"
          className="flex flex-col items-center justify-center gap-1 min-h-14 pt-2 pb-1 cursor-pointer"
          style={{ color: C.textPrimary }}
        >
          <span
            className="flex items-center justify-center w-14 h-8 rounded-full"
            style={{ background: C.accent, color: C.onAccent }}
          >
            <Plus size={20} strokeWidth={2.25} aria-hidden />
          </span>
          <span className="text-xs leading-none" style={{ fontWeight: 600 }}>
            {t("newItem")}
          </span>
        </button>
        {tab("chats", chatsTabHref(pathname, recent))}
        {tab("inbox", TAB_HREFS.inbox)}
      </div>
    </nav>
  );
}
