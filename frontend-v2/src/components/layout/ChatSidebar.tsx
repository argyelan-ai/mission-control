"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useTranslations } from "next-intl";
import { LayoutGrid } from "lucide-react";
import { C, P2 } from "@/lib/colors";
import { activeTabFor, TAB_HREFS, TAB_ICONS, TAB_LABEL_KEYS, TAB_ORDER } from "@/lib/mobileNav";
import { badgeLabel } from "@/lib/inbox";
import { useInbox } from "@/hooks/useInbox";
import { NavVoiceButton } from "@/components/voice/live/NavVoiceButton";
import { ChatList } from "./ChatList";
import SidebarFooter from "./SidebarFooter";

// Wordmark (same env-driven logic as the phone app bar)
const _BRAND = process.env.NEXT_PUBLIC_BRAND || "Mission.Control";
const _dot = _BRAND.lastIndexOf(".");
const BRAND_MAIN = _dot > 0 ? _BRAND.slice(0, _dot) : _BRAND;
const BRAND_ACCENT = _dot > 0 ? _BRAND.slice(_dot) : "";

/** Settings › More: every page that left the main navigation. */
export const MORE_HREF = "/settings?section=more";

/**
 * ChatSidebar — the desktop's one column (operator decision 2026-11-18,
 * variant C + A on the desktop): the same chat list as the phone drawer,
 * always visible, and the three destinations Chat · Models · Knowledge at its
 * foot. More (every other page) and Settings sit in the last row with the
 * user. Replaces the page tree of Shell v4 in the shell; that sidebar
 * (Sidebar.tsx) stays in the code, unmounted.
 */
export function ChatSidebar() {
  const t = useTranslations("cockpit");
  const pathname = usePathname() ?? "/";
  const active = activeTabFor(pathname);
  const { count } = useInbox();
  const badge = badgeLabel(count);

  return (
    <aside
      data-testid="chat-sidebar"
      aria-label={t("chatList")}
      className="flex flex-col h-full w-64 rounded-xl overflow-hidden"
      style={{ background: "var(--color-p2-pan)", border: "1px solid var(--color-p2-line2)" }}
    >
      <div className="flex items-center justify-between gap-2 px-4 pt-4 pb-3 shrink-0">
        <Link
          href="/"
          className="flex items-center h-9 text-base font-bold"
          style={{ color: "var(--color-p2-txt)", fontFamily: "var(--font-p2-display)" }}
        >
          {BRAND_MAIN}
          <span style={{ color: P2.amb }}>{BRAND_ACCENT}</span>
        </Link>
        <NavVoiceButton size={30} variant="sidebar" enforceTouchTarget={false} />
      </div>

      <div className="flex-1 min-h-0 overflow-y-auto px-2 pb-3">
        <ChatList />
      </div>

      <nav aria-label={t("mainNav")} className="shrink-0 px-2 pt-2 flex flex-col" style={{ borderTop: "1px solid var(--color-p2-line2)" }}>
        {TAB_ORDER.map((key) => {
          const Icon = TAB_ICONS[key];
          const on = active === key;
          const rowBadge = key === "chat" ? badge : null;
          return (
            <Link
              key={key}
              href={TAB_HREFS[key]}
              data-tab={key}
              aria-current={on ? "page" : undefined}
              className="flex items-center gap-3 min-h-10 px-3 rounded-lg text-sm transition-colors hover:bg-[var(--color-bg-hover)]"
              style={{
                color: on ? C.textPrimary : C.textSecondary,
                fontWeight: on ? 500 : 400,
                ...(on ? { background: C.accentSubtle } : null),
              }}
            >
              <Icon size={17} strokeWidth={on ? 2 : 1.75} aria-hidden />
              <span className="flex-1">{t(TAB_LABEL_KEYS[key])}</span>
              {rowBadge && (
                <span
                  data-testid="chat-sidebar-badge"
                  aria-label={t("chatWaiting", { count })}
                  className="text-xs tabular-nums"
                  style={{ color: C.textMuted }}
                >
                  {rowBadge}
                </span>
              )}
            </Link>
          );
        })}
        <Link
          href={MORE_HREF}
          className="flex items-center gap-3 min-h-10 px-3 rounded-lg text-sm transition-colors hover:bg-[var(--color-bg-hover)]"
          style={{ color: C.textMuted }}
        >
          <LayoutGrid size={17} strokeWidth={1.75} aria-hidden />
          {t("more")}
        </Link>
      </nav>

      <div className="shrink-0 px-3 pb-3">
        <SidebarFooter collapsible={false} />
      </div>
    </aside>
  );
}
