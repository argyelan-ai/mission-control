"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useLocale, useTranslations } from "next-intl";
import { SquarePen } from "lucide-react";
import { C } from "@/lib/colors";
import { chatRowTime, type ChatListEntry, type ChatListState } from "@/lib/chatList";
import { useRecentChats } from "./MobileTabBar";
import { activeChatKey, useChatList, useOpenChat } from "./useChatList";

/** Where "New chat" leads: the sessions page's picker — who to talk to, or a
 *  new group. Until chats stop being bound to agents (ADR-102 stage 1) a new
 *  chat starts by picking the agent. */
export const NEW_CHAT_HREF = "/sessions?pick=1";

/** One muted dot for state, nothing at all for a quiet chat. */
function StateDot({ state, label }: { state: ChatListState; label: string }) {
  if (state === "idle") return <span aria-hidden className="w-1.5 shrink-0" />;
  return (
    <span
      role="img"
      aria-label={label}
      data-state={state}
      className="w-1.5 h-1.5 rounded-full shrink-0"
      style={{ background: state === "waiting" ? C.accent : C.info }}
    />
  );
}

function Row({ entry, active, onNavigate }: { entry: ChatListEntry; active: boolean; onNavigate?: () => void }) {
  const t = useTranslations("cockpit");
  const locale = useLocale();
  const time = chatRowTime(entry.at, locale);
  return (
    <li>
      <Link
        href={entry.href}
        onClick={onNavigate}
        aria-current={active ? "page" : undefined}
        data-testid="chat-list-row"
        data-chat={entry.key}
        className="flex items-center gap-2.5 min-h-11 px-3 rounded-lg transition-colors hover:bg-[var(--color-bg-hover)]"
        style={active ? { background: C.accentSubtle } : undefined}
      >
        <StateDot state={entry.state} label={entry.state === "waiting" ? t("stateWaiting") : t("stateWorking")} />
        <span
          className="flex-1 min-w-0 truncate text-sm"
          style={{ color: active ? C.textPrimary : C.textSecondary, fontWeight: active ? 500 : 400 }}
        >
          {entry.ordinal ? t("furtherChat", { name: entry.title, n: entry.ordinal }) : entry.title}
        </span>
        {time && (
          <span className="shrink-0 text-xs tabular-nums" style={{ color: C.textMuted }}>
            {time}
          </span>
        )}
      </Link>
    </li>
  );
}

function Section({
  label,
  entries,
  activeKey,
  onNavigate,
  testId,
}: {
  label: string;
  entries: ChatListEntry[];
  activeKey: string | null;
  onNavigate?: () => void;
  testId: string;
}) {
  if (entries.length === 0) return null;
  return (
    <section data-testid={testId} aria-label={label} className="flex flex-col gap-1">
      <h3 className="label-sys px-3 pt-2 pb-1" style={{ color: C.textMuted }}>
        {label}
      </h3>
      <ul className="flex flex-col">
        {entries.map((e) => (
          <Row key={e.key} entry={e} active={e.key === activeKey} onNavigate={onNavigate} />
        ))}
      </ul>
    </section>
  );
}

/**
 * The chat list of the shell — the same list in the phone drawer and in the
 * desktop column (operator decision 2026-11-18, variant A's menu): "New chat"
 * on top, then "Waiting for you", "Today", "Earlier". A calm list: title and
 * time, one muted dot for state, no boxes and no runtime badges. The caller
 * puts its own destinations underneath.
 */
export function ChatList({ onNavigate }: { onNavigate?: () => void }) {
  const t = useTranslations("cockpit");
  const pathname = usePathname() ?? "/";
  const { waiting, today, earlier, loaded } = useChatList();
  const recent = useRecentChats();
  const openChat = useOpenChat();
  // The open chat is the one the sessions page remembered last (it writes
  // lib/recentChats on every pick and deep link) — or, while one is open,
  // the agent's further chat (its own row, not the agent's).
  const activeKey = activeChatKey(pathname, recent[0], openChat);
  const empty = loaded && waiting.length + today.length + earlier.length === 0;

  return (
    <div className="flex flex-col gap-3 min-h-0" data-testid="chat-list">
      <Link
        href={NEW_CHAT_HREF}
        onClick={onNavigate}
        data-testid="chat-list-new"
        className="flex items-center gap-2.5 min-h-11 px-3 rounded-lg text-sm transition-colors hover:bg-[var(--color-bg-hover)]"
        style={{ color: C.textPrimary, border: `1px solid ${C.borderActive}`, fontWeight: 500 }}
      >
        <SquarePen size={16} aria-hidden />
        {t("newChat")}
      </Link>
      <Section label={t("sectionWaiting")} entries={waiting} activeKey={activeKey} onNavigate={onNavigate} testId="chat-section-waiting" />
      <Section label={t("sectionToday")} entries={today} activeKey={activeKey} onNavigate={onNavigate} testId="chat-section-today" />
      <Section label={t("sectionEarlier")} entries={earlier} activeKey={activeKey} onNavigate={onNavigate} testId="chat-section-earlier" />
      {empty && (
        <p className="px-3 text-sm" style={{ color: C.textMuted }}>
          {t("emptyChats")}
        </p>
      )}
    </div>
  );
}
