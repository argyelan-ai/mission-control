"use client";

import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore, type ReactNode } from "react";
import { Command } from "cmdk";
import { useRouter } from "next/navigation";
import { motion, AnimatePresence, useReducedMotion } from "framer-motion";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { AudioLines, MessageSquare, MessageSquareText, Search, Settings, SquarePen, Users, type LucideIcon } from "lucide-react";
import { useLocale, useTranslations } from "next-intl";
import { NAV_TREE, type NavItem } from "@/lib/nav";
import { useAppStore } from "@/lib/store";
import { api, type RecordChat, type RecordSearchHit } from "@/lib/api";
import { chatRowTime, type ChatListEntry } from "@/lib/chatList";
import { useBodyScrollLock } from "@/hooks/useBodyScrollLock";
import { useChatList } from "@/components/layout/useChatList";
// One link for both "New chat" doors (list button + palette): the draft (P4).
import { NEW_CHAT_HREF } from "@/components/layout/ChatList";
import { useVoiceContext } from "@/components/voice/VoiceWidget";
import { useVoiceLiveCall } from "@/components/voice/live/VoiceLiveCall";
import { C, alpha } from "@/lib/colors";
import {
  GROUP_MAX,
  MESSAGE_SEARCH_DEBOUNCE_MS,
  messageHitTarget,
  rankByName,
  rankChats,
  recentChats,
  splitHighlight,
  wantsMessageSearch,
} from "./paletteSearch";

/** Is this error "the route is not there" (an older backend)? Only that
 *  and `enabled: false` switch the message search off — a 5xx, a network
 *  flap or an expired login only empty that one query. */
function isMissingRoute(error: unknown): boolean {
  return error instanceof Error && /^API 404\b/.test(error.message);
}

/** "The record's search is off here" — kept in the query cache (not in the
 *  box's own state) so the next open neither asks nor flashes "Searching…".
 *  A reload asks again. */
const SEARCH_OFF_KEY = ["chat-record", "search-off"] as const;

// ── Styles (tokens only) ────────────────────────────────────────────────────
// Selected = accent-subtle surface + 2 px accent bar on the left (inset, no
// glow). Rows are 44 px finger targets (K11).
const itemClass =
  "flex items-center gap-3 min-h-11 px-3 rounded-md text-sm cursor-pointer transition-colors " +
  "data-[selected=true]:bg-[var(--color-accent-subtle)] " +
  "data-[selected=true]:shadow-[inset_2px_0_0_0_var(--color-accent)]";

const groupClass =
  "[&_[cmdk-group-heading]]:px-3 [&_[cmdk-group-heading]]:pt-3 [&_[cmdk-group-heading]]:pb-1";

const PHONE_QUERY = "(max-width: 767px)";

function subscribePhone(cb: () => void): () => void {
  try {
    const mq = window.matchMedia(PHONE_QUERY);
    mq.addEventListener("change", cb);
    return () => mq.removeEventListener("change", cb);
  } catch {
    return () => {};
  }
}

function isPhone(): boolean {
  try {
    return window.matchMedia(PHONE_QUERY).matches;
  } catch {
    return false;
  }
}

/** Below Tailwind's `md` the palette fills the screen (concept 5.2). */
function usePhone(): boolean {
  return useSyncExternalStore(subscribePhone, isPhone, () => false);
}

function useDebounced(value: string, ms: number): string {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const id = setTimeout(() => setDebounced(value), ms);
    return () => clearTimeout(id);
  }, [value, ms]);
  return debounced;
}

/**
 * The search box (concept "Neuer Chat und Suche", P5) — ⌘K / Ctrl-K and the
 * search row of the desktop column; on the phone the magnifier in the chat
 * drawer, full screen. Empty = the recent chats (pinned first). Typing =
 * Chats (title + agent, in the browser) → In messages (the record's search,
 * from 3 letters, 250 ms after typing stops) → Commands → Pages. An agent
 * opens its chat; its settings are the row's second action.
 */
export default function CommandPalette() {
  const { commandPaletteOpen, setCommandPaletteOpen } = useAppStore();

  // Body-Scroll-Lock: verhindert Hintergrund-Scrolling auf iOS (MOBILE-SPEC M4)
  useBodyScrollLock(commandPaletteOpen);

  const close = useCallback(() => setCommandPaletteOpen(false), [setCommandPaletteOpen]);

  return <AnimatePresence>{commandPaletteOpen && <PaletteLayer key="palette" onClose={close} />}</AnimatePresence>;
}

/** Mounted only while open: the chat list, the record and the search are
 *  asked only then, and the box starts empty every time. */
function PaletteLayer({ onClose }: { onClose: () => void }) {
  const t = useTranslations("shell");
  const tNav = useTranslations("nav");
  const tCockpit = useTranslations("cockpit");
  const locale = useLocale();
  const router = useRouter();
  const phone = usePhone();
  const reduce = useReducedMotion();
  const jarvisInMenu = !!useVoiceLiveCall()?.lead;
  const inputRef = useRef<HTMLInputElement>(null);

  const [query, setQuery] = useState("");
  const debounced = useDebounced(query.trim(), MESSAGE_SEARCH_DEBOUNCE_MS);
  const typing = query.trim().length > 0;

  // Focus after the opener let go: the phone drawer closes in the same
  // click and hands focus back to its button on unmount.
  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  const navigate = useCallback(
    (href: string) => {
      router.push(href);
      onClose();
    },
    [router, onClose],
  );

  // ── Chats: the chat list's own rows (drawer + desktop column) ────────────
  const { waiting, today, earlier } = useChatList();
  const entries = useMemo(() => [...waiting, ...today, ...earlier], [waiting, today, earlier]);
  const { data: record } = useQuery({
    queryKey: ["chat-record", "chats"],
    queryFn: () => api.chatRecord.listChats(),
    retry: false,
  });
  const recordChats: RecordChat[] = useMemo(() => (Array.isArray(record?.chats) ? record.chats : []), [record]);

  const agentTitle = useMemo(() => {
    const m = new Map<string, string>();
    for (const e of entries) if (e.kind === "agent" && !e.chatId) m.set(e.id, e.title);
    return m;
  }, [entries]);
  const titleOf = useCallback(
    (e: ChatListEntry) => (e.ordinal ? tCockpit("furtherChat", { name: e.title, n: e.ordinal }) : e.title),
    [tCockpit],
  );
  const agentNameOf = useCallback(
    (e: ChatListEntry) => {
      if (!e.chatId) return null;
      const name = agentTitle.get(e.id);
      return name && name !== e.title ? name : null;
    },
    [agentTitle],
  );

  const chatRows = useMemo(
    () => (typing ? rankChats(entries, query, titleOf, agentNameOf).slice(0, GROUP_MAX) : recentChats(entries, recordChats)),
    [typing, entries, query, titleOf, agentNameOf, recordChats],
  );

  // ── In messages: the record's search, quiet when it is off or missing ───
  const queryClient = useQueryClient();
  const { data: recordOff = false } = useQuery({
    queryKey: SEARCH_OFF_KEY,
    queryFn: () => false,
    initialData: false,
    staleTime: Infinity,
    gcTime: Infinity,
  });
  const askServer = wantsMessageSearch(debounced) && !recordOff;
  const search = useQuery({
    queryKey: ["chat-record", "search", debounced],
    queryFn: () => api.chatRecord.search(debounced, GROUP_MAX),
    enabled: askServer,
    retry: false,
    staleTime: 30_000,
  });
  useEffect(() => {
    // Off (enabled: false) or an older backend without the route: hide the
    // group and stop asking until the page reloads.
    if (search.data?.enabled === false || (search.isError && isMissingRoute(search.error))) {
      queryClient.setQueryData(SEARCH_OFF_KEY, true);
    }
  }, [search.data, search.isError, search.error, queryClient]);
  const hits = useMemo(() => {
    if (!wantsMessageSearch(query) || recordOff || search.isError || !search.data?.enabled) return [];
    const out: { hit: RecordSearchHit; target: ChatListEntry }[] = [];
    for (const hit of search.data.hits ?? []) {
      const target = messageHitTarget(hit, entries, recordChats);
      if (target) out.push({ hit, target });
    }
    return out;
  }, [query, recordOff, search.isError, search.data, entries, recordChats]);
  const searching =
    wantsMessageSearch(query) && !recordOff && (debounced !== query.trim() || search.isFetching) && hits.length === 0;

  // ── Commands: only ones that do something ────────────────────────────────
  type Cmd = { key: string; label: string; names: string[]; icon: LucideIcon; run: () => void; testId?: string };
  const commands = useMemo(() => {
    const list: Cmd[] = [
      {
        key: "new-chat",
        label: tCockpit("newChat"),
        names: [tCockpit("newChat"), "new chat", "neuer chat"],
        icon: SquarePen,
        run: () => navigate(NEW_CHAT_HREF),
      },
    ];
    return typing ? rankByName(list, query, (c) => c.names) : list;
  }, [tCockpit, navigate, typing, query]);
  const jarvisShown = jarvisInMenu && (!typing || rankByName([0], query, () => ["jarvis", "voice", "sprache"]).length > 0);

  // ── Pages: last, only while typing ───────────────────────────────────────
  const pages = useMemo(() => {
    if (!typing) return [];
    const leaves: NavItem[] = NAV_TREE.flatMap((g) => g.children);
    return rankByName(leaves, query, (p) => [tNav(p.labelKey) || p.label, p.label]).slice(0, GROUP_MAX);
  }, [typing, query, tNav]);

  const total = chatRows.length + hits.length + commands.length + (jarvisShown ? 1 : 0) + pages.length;

  const heading = (text: string) => <span className="label-sys">{text}</span>;

  const box = (
    <Command
      shouldFilter={false}
      label={t("searchDialog")}
      className="flex flex-col min-h-0 h-full"
      onKeyDown={(e) => {
        if (e.key === "Escape") {
          e.preventDefault();
          onClose();
        }
      }}
    >
      {/* palette-input-row (globals.css): the field's focus shows as the
          row's accent underline, not as the global ring around the field. */}
      <div className="palette-input-row flex items-center gap-3 px-4 shrink-0">
        <Search size={16} aria-hidden style={{ color: C.textMuted, flexShrink: 0 }} />
        {/* 16 px text: iOS zooms into any smaller field. */}
        <Command.Input
          ref={inputRef}
          autoFocus
          value={query}
          onValueChange={setQuery}
          placeholder={t("searchPlaceholder")}
          className="flex-1 min-w-0 min-h-12 bg-transparent text-base outline-none"
          style={{ color: C.textPrimary, caretColor: C.accent }}
        />
        {phone ? (
          <button
            type="button"
            onClick={onClose}
            data-testid="palette-cancel"
            className="shrink-0 min-h-11 px-1 text-sm cursor-pointer"
            style={{ color: C.textSecondary }}
          >
            {t("searchCancel")}
          </button>
        ) : (
          <kbd
            className="font-mono text-xs px-1.5 py-0.5 rounded-sm shrink-0"
            style={{ backgroundColor: "var(--color-bg-deep)", color: C.textMuted, border: `1px solid ${C.border}` }}
          >
            Esc
          </kbd>
        )}
      </div>

      <Command.List
        className={`overflow-y-auto px-2 pb-2 ${phone ? "flex-1 min-h-0" : "max-h-[60dvh]"}`}
        style={{ color: C.textPrimary }}
      >
        {chatRows.length > 0 && (
          <Command.Group
            heading={heading(typing ? t("searchGroupChats") : t("searchGroupRecent"))}
            className={groupClass}
            data-testid={typing ? "palette-group-chats" : "palette-group-recent"}
          >
            {chatRows.map((e) => (
              <ChatItem
                key={e.key}
                entry={e}
                title={titleOf(e)}
                agentName={agentNameOf(e)}
                time={chatRowTime(e.at, locale)}
                // only while searching: in the recent list a gear on every
                // agent row would be noise (K4)
                settingsLabel={typing && e.kind === "agent" && !e.chatId ? t("searchAgentSettings", { name: e.title }) : null}
                gearSlot={typing}
                onOpen={() => navigate(e.href)}
                onSettings={() => navigate(`/agents/${e.id}`)}
              />
            ))}
          </Command.Group>
        )}

        {(hits.length > 0 || searching) && (
          <Command.Group heading={heading(t("searchGroupMessages"))} className={groupClass} data-testid="palette-group-messages">
            {hits.map(({ hit, target }) => (
              <MessageHitItem key={hit.chatId} hit={hit} title={titleOf(target)} onOpen={() => navigate(target.href)} />
            ))}
            {searching && (
              <Command.Loading>
                <p className="px-3 py-2 text-sm" style={{ color: C.textMuted }}>
                  {t("searchSearching")}
                </p>
              </Command.Loading>
            )}
          </Command.Group>
        )}

        {(commands.length > 0 || jarvisShown) && (
          <Command.Group heading={heading(t("searchGroupCommands"))} className={groupClass} data-testid="palette-group-commands">
            {commands.map((c) => (
              <Command.Item key={c.key} value={`command:${c.key}`} onSelect={c.run} className={itemClass}>
                <c.icon size={16} aria-hidden style={{ color: C.textSecondary }} />
                <span style={{ color: C.textPrimary }}>{c.label}</span>
              </Command.Item>
            ))}
            {/* Jarvis lives here on the desktop since voice PR 5: the
                sidebar mic calls the lead agent now. Only while it does —
                with the live voice off, that mic IS Jarvis. */}
            {jarvisShown && <JarvisPaletteItem onDone={onClose} />}
          </Command.Group>
        )}

        {pages.length > 0 && (
          <Command.Group heading={heading(t("searchGroupPages"))} className={groupClass} data-testid="palette-group-pages">
            {pages.map(({ icon: Icon, labelKey, label, href }) => (
              <Command.Item key={href} value={`page:${href}`} onSelect={() => navigate(href)} className={itemClass}>
                <Icon size={16} aria-hidden style={{ color: C.textSecondary }} />
                <span style={{ color: C.textPrimary }}>{tNav(labelKey) || label}</span>
              </Command.Item>
            ))}
          </Command.Group>
        )}

        {typing && total === 0 && !searching && (
          <p className="py-8 text-center text-sm" style={{ color: C.textMuted }}>
            {t("noResults")}
          </p>
        )}
      </Command.List>
    </Command>
  );

  if (phone) {
    return (
      <motion.div
        role="dialog"
        aria-modal="true"
        aria-label={t("searchDialog")}
        data-testid="palette"
        data-layout="phone"
        className="fixed inset-0 z-50 flex flex-col pt-island pb-safe-bottom"
        style={{ backgroundColor: "var(--color-bg-elevated)" }}
        initial={{ opacity: 0, y: 8 }}
        animate={{ opacity: 1, y: 0 }}
        exit={{ opacity: 0, y: 8 }}
        transition={{ duration: reduce ? 0 : 0.18, ease: [0.16, 1, 0.3, 1] }}
      >
        {box}
      </motion.div>
    );
  }

  return (
    <>
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        transition={{ duration: reduce ? 0 : 0.15 }}
        className="fixed inset-0 z-50"
        style={{ backgroundColor: alpha(C.scrim, 0.7) }}
        onClick={onClose}
      />
      <motion.div
        role="dialog"
        aria-modal="true"
        aria-label={t("searchDialog")}
        data-testid="palette"
        data-layout="desktop"
        initial={{ opacity: 0, scale: 0.96, y: -10, x: "-50%" }}
        animate={{ opacity: 1, scale: 1, y: 0, x: "-50%" }}
        exit={{ opacity: 0, scale: 0.96, y: -10, x: "-50%" }}
        transition={{ duration: reduce ? 0 : 0.2, ease: [0.16, 1, 0.3, 1] }}
        className="left-1/2 z-50 w-[calc(100%-2rem)] max-w-lg rounded-md overflow-hidden corner-ticks"
        style={{
          // Two things fight the centring here, so both are handled
          // explicitly: framer writes `transform` inline and would drop a
          // class-based -translate-x-1/2 (hence x in the motion props), and
          // `.corner-ticks { position: relative }` in globals.css lands
          // after the Tailwind utilities and beats a `fixed` class.
          position: "fixed",
          top: "calc(env(safe-area-inset-top) + 1rem)",
          backgroundColor: "var(--color-bg-elevated)",
          border: `1px solid ${C.border}`,
          boxShadow: "var(--shadow-elevated)",
        }}
      >
        {box}
      </motion.div>
    </>
  );
}

const KIND_ICON: Record<string, LucideIcon> = { group: Users };

/** One chat: title (+ the agent's name for a titled further chat), the
 *  list's quiet time, and for an agent's own chat its settings as a second
 *  action. */
function ChatItem({
  entry,
  title,
  agentName,
  time,
  settingsLabel,
  gearSlot,
  onOpen,
  onSettings,
}: {
  entry: ChatListEntry;
  title: string;
  agentName: string | null;
  time: string | null;
  settingsLabel: string | null;
  /** While searching some rows carry the gear: every row keeps its slot, so
   *  the times stand in one column (K-alignment). */
  gearSlot: boolean;
  onOpen: () => void;
  onSettings: () => void;
}) {
  const Icon = KIND_ICON[entry.kind] ?? MessageSquare;
  return (
    <Command.Item value={`chat:${entry.key}`} onSelect={onOpen} className={itemClass} data-testid="palette-chat" data-key={entry.key}>
      <Icon size={16} aria-hidden style={{ color: C.textMuted, flexShrink: 0 }} />
      <span className="flex-1 min-w-0 truncate">
        <span style={{ color: C.textPrimary }}>{title}</span>
        {agentName && <span style={{ color: C.textMuted }}>{` · ${agentName}`}</span>}
      </span>
      {time && (
        <span className="shrink-0 text-xs tabular-nums" style={{ color: C.textMuted }}>
          {time}
        </span>
      )}
      {settingsLabel && (
        <button
          type="button"
          aria-label={settingsLabel}
          title={settingsLabel}
          data-testid="palette-agent-settings"
          onPointerDown={(e) => e.stopPropagation()}
          onClick={(e) => {
            e.stopPropagation();
            onSettings();
          }}
          className="shrink-0 flex items-center justify-center w-11 h-11 -mr-3 rounded-md cursor-pointer transition-colors hover:bg-[var(--color-bg-hover)]"
          style={{ color: C.textMuted }}
        >
          <Settings size={16} aria-hidden />
        </button>
      )}
      {!settingsLabel && gearSlot && <span aria-hidden data-testid="palette-gear-slot" className="shrink-0 w-11 h-11 -mr-3" />}
    </Command.Item>
  );
}

/** One chat that holds the words: its name, then the best message with the
 *  match marked. Opens the chat (the chat view has no message anchor yet). */
function MessageHitItem({ hit, title, onOpen }: { hit: RecordSearchHit; title: string; onOpen: () => void }) {
  const { before, match, after } = splitHighlight(hit.snippet, hit.highlight);
  let marked: ReactNode = before;
  if (match) {
    marked = (
      <>
        {before}
        <mark className="rounded-sm" style={{ background: C.accentSubtle, color: C.textPrimary }}>
          {match}
        </mark>
        {after}
      </>
    );
  }
  return (
    <Command.Item
      value={`message:${hit.chatId}`}
      onSelect={onOpen}
      className={`${itemClass} items-start py-2`}
      data-testid="palette-message-hit"
    >
      <span className="flex h-5 shrink-0 items-center" aria-hidden>
        <MessageSquareText size={16} style={{ color: C.textMuted }} />
      </span>
      <span className="flex-1 min-w-0 flex flex-col gap-0.5">
        <span className="truncate" style={{ color: C.textPrimary }}>
          {title}
        </span>
        <span className="text-xs line-clamp-2" style={{ color: C.textSecondary }}>
          {marked}
        </span>
      </span>
    </Command.Item>
  );
}

/** Jarvis (LiveKit, unchanged) from the palette: same toggle as its old mic —
 *  starts a session, or opens the running one's panel. */
function JarvisPaletteItem({ onDone }: { onDone: () => void }) {
  const t = useTranslations("voiceLive");
  const voice = useVoiceContext();
  return (
    <Command.Item
      value="command:jarvis"
      data-testid="palette-jarvis"
      onSelect={() => {
        onDone();
        voice.toggleButton();
      }}
      className={itemClass}
    >
      <AudioLines size={16} aria-hidden style={{ color: C.textSecondary }} />
      <span style={{ color: C.textPrimary }}>{t("jarvisClassic")}</span>
    </Command.Item>
  );
}
