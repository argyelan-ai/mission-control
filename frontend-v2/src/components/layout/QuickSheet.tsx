"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { useQuery } from "@tanstack/react-query";
import { AnimatePresence, motion, useReducedMotion, type PanInfo } from "framer-motion";
import {
  ChevronLeft,
  ChevronRight,
  LayoutGrid,
  MessagesSquare,
  Mic,
  Play,
  Settings,
  type LucideIcon,
} from "lucide-react";
import { api } from "@/lib/api";
import { useAppStore } from "@/lib/store";
import { C, alpha } from "@/lib/colors";
import { sheetPages } from "@/lib/mobileNav";
import type { NavItem } from "@/lib/nav";
import { chatHref, type ChatRef } from "@/lib/recentChats";
import { CreateTaskModal } from "@/components/shared/CreateTaskModal";
import { useVoiceContext } from "@/components/voice/VoiceWidget";
import { useMobileNav } from "./MobileNav";
import { useRecentChats } from "./MobileTabBar";

type Named = { id: string; name: string };

/**
 * Resolve the device's recent chats to names. A chat whose agent or group is
 * gone (deleted, renamed id) is skipped instead of shown as a raw id.
 */
export function resolveRecentChats(
  recent: ChatRef[],
  agents: Named[],
  groups: Named[],
  max = 1,
): { ref: ChatRef; name: string }[] {
  const byAgent = new Map(agents.map((a) => [a.id, a.name]));
  const byGroup = new Map(groups.map((g) => [g.id, g.name]));
  const out: { ref: ChatRef; name: string }[] = [];
  for (const ref of recent) {
    const name = ref.kind === "agent" ? byAgent.get(ref.id) : byGroup.get(ref.id);
    if (name) out.push({ ref, name });
    if (out.length >= max) break;
  }
  return out;
}

const rowClass =
  "w-full flex items-center gap-4 px-1 rounded-lg text-left cursor-pointer transition-colors hover:bg-[var(--color-bg-hover)] disabled:cursor-not-allowed disabled:opacity-50";

/** Icon column + label (+ hint) + optional trailing chevron. One row shape for every entry. */
function RowBody({
  icon: Icon,
  label,
  hint,
  chevron = false,
}: {
  icon: LucideIcon;
  label: string;
  hint?: string;
  chevron?: boolean;
}) {
  return (
    <>
      <span className="shrink-0 w-10 -ml-1 flex justify-center">
        <Icon size={20} aria-hidden style={{ color: C.textSecondary }} />
      </span>
      <span className="flex-1 min-w-0">
        <span className="block text-base truncate" style={{ color: C.textPrimary }}>
          {label}
        </span>
        {hint && (
          <span className="block text-sm truncate" style={{ color: C.textMuted }}>
            {hint}
          </span>
        )}
      </span>
      {chevron && <ChevronRight size={18} aria-hidden className="shrink-0" style={{ color: C.textMuted }} />}
    </>
  );
}

function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <div className="text-sm px-1 pt-5 pb-1" style={{ color: C.textMuted }}>
      {children}
    </div>
  );
}

/**
 * QuickSheet — the phone menu behind the ⊕ in the tab bar. Variant B
 * „Zwei Ebenen" (operator decision 2026-10-02, concept mobile-menu-v3):
 *
 *   Level 1 (start something)        Level 2 „Pages" (go somewhere)
 *   ▶ New job          (primary)     ‹ Pages
 *   🎙 Voice command                   Used most: Runtimes · Agents · …
 *   💬 Continue with <last chat>      Others:    the rest, alphabetical
 *   ─────
 *   ▦ Pages ›
 *   ⚙ Settings ›   (board, account and log out live there on the phone)
 *
 * Level 2 is generated from lib/nav.ts (sheetPages) — no second list. Back:
 * the ‹ button, a swipe to the right, or Esc. One column of calm list rows,
 * one accent surface (New job, K11), no theme switch (Settings › Appearance),
 * no visible title or ✕ — the sheet closes by swiping down, tapping beside
 * it or Esc; a visually hidden close button remains for screen readers.
 */
export function QuickSheet() {
  const t = useTranslations("nav");
  const router = useRouter();
  const reduce = useReducedMotion();
  const { quickOpen, setQuickOpen, quickLevel, setQuickLevel } = useMobileNav();
  const { activeBoardId } = useAppStore();
  const voice = useVoiceContext();
  const recent = useRecentChats();
  const [newJobRequest, setNewJobRequest] = useState(0);
  const firstRef = useRef<HTMLButtonElement>(null);
  const backRef = useRef<HTMLButtonElement>(null);
  const pagesRowRef = useRef<HTMLButtonElement>(null);
  const prevLevel = useRef(quickLevel);

  // The last chat's name — the same queries (and cache) as the chats page.
  const { data: dockerAgents = [] } = useQuery({
    queryKey: ["agents", "docker-sessions"],
    queryFn: () => api.agents.listDockerSessions(),
    enabled: quickOpen && recent.length > 0,
  });
  const { data: hostAgents = [] } = useQuery({
    queryKey: ["agents", "host-sessions"],
    queryFn: () => api.agents.listHostSessions(),
    enabled: quickOpen && recent.length > 0,
  });
  const { data: groups = [] } = useQuery({
    queryKey: ["groups"],
    queryFn: () => api.groups.list({ includeArchived: true }),
    enabled: quickOpen && recent.some((r) => r.kind === "group"),
  });
  // The New-task modal needs the board's agents (assignee list).
  const { data: boardAgents } = useQuery({
    queryKey: ["agents", activeBoardId],
    queryFn: () => api.agents.list(activeBoardId ?? undefined),
    enabled: !!activeBoardId && (quickOpen || newJobRequest > 0),
  });

  const lastChat = resolveRecentChats(recent, [...dockerAgents, ...hostAgents], groups)[0];

  const labelOf = (i: NavItem) => t(i.labelKey) || i.label;
  const pages = sheetPages(labelOf);

  // Focus: the first row on open; the back button on level 2; the Pages row
  // again when coming back — the keyboard and screen reader never get lost.
  useEffect(() => {
    if (!quickOpen) return;
    const from = prevLevel.current;
    prevLevel.current = quickLevel;
    const target =
      quickLevel === "pages" ? backRef : from === "pages" ? pagesRowRef : firstRef;
    const id = window.setTimeout(() => target.current?.focus(), 50);
    return () => window.clearTimeout(id);
  }, [quickOpen, quickLevel]);

  const close = () => setQuickOpen(false);

  function onDragEnd(_: unknown, info: PanInfo) {
    if (info.offset.y > 80 || info.velocity.y > 600) close();
  }

  // Swipe to the right on level 2 = back (the iOS back gesture inside the sheet).
  function onPagesPanEnd(_: unknown, info: PanInfo) {
    if (info.offset.x > 80 && Math.abs(info.offset.y) < 60) setQuickLevel("root");
  }

  const levelMotion = reduce
    ? {}
    : {
        initial: { opacity: 0, x: quickLevel === "pages" ? 24 : -24 },
        animate: { opacity: 1, x: 0 },
        transition: { duration: 0.2, ease: [0.16, 1, 0.3, 1] as const },
      };

  const pageRow = (item: NavItem) => (
    <Link
      key={item.href}
      href={item.href}
      onClick={close}
      className={`${rowClass} min-h-12`}
      data-page={item.href}
    >
      <RowBody icon={item.icon} label={labelOf(item)} />
    </Link>
  );

  return (
    <>
      <AnimatePresence>
        {quickOpen && (
          <>
            <motion.div
              key="quick-scrim"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              transition={{ duration: reduce ? 0 : 0.2 }}
              className="fixed inset-0 z-40 md:hidden"
              style={{ background: alpha(C.scrim, 0.75) }}
              onClick={close}
              aria-hidden="true"
            />
            <motion.div
              key="quick-sheet"
              role="dialog"
              aria-modal="true"
              aria-label={quickLevel === "pages" ? t("quick.pages") : t("quick.title")}
              data-testid="quick-sheet"
              data-level={quickLevel}
              initial={reduce ? { opacity: 0 } : { y: "100%" }}
              animate={reduce ? { opacity: 1 } : { y: 0 }}
              exit={reduce ? { opacity: 0 } : { y: "100%" }}
              transition={{ duration: reduce ? 0 : 0.3, ease: [0.16, 1, 0.3, 1] }}
              drag={reduce ? false : "y"}
              dragConstraints={{ top: 0, bottom: 0 }}
              dragElastic={{ top: 0, bottom: 0.6 }}
              onDragEnd={onDragEnd}
              className="fixed inset-x-0 bottom-0 z-50 md:hidden px-4 rounded-t-2xl"
              style={{
                background: C.bgElevated,
                // 2px accent edge: this system's "this is an overlay" mark (DESIGN.md).
                borderTop: `2px solid ${C.accent}`,
                boxShadow: "var(--shadow-elevated)",
                paddingBottom: "calc(env(safe-area-inset-bottom) + 0.75rem)",
                maxHeight: "calc(100dvh - env(safe-area-inset-top) - 1rem)",
                overflowY: "auto",
              }}
            >
              {/* No visible ✕ (concept v3): swipe down, tap beside or Esc. */}
              <button type="button" className="sr-only" onClick={close}>
                {t("quick.close")}
              </button>
              {/* Grabber — tells the thumb it can pull the sheet down. */}
              <div className="flex justify-center pt-2 pb-1" aria-hidden>
                <span className="w-10 h-1 rounded-full" style={{ background: C.borderActive }} />
              </div>

              {quickLevel === "root" ? (
                <motion.div key="root" {...levelMotion} className="flex flex-col pt-2" data-testid="quick-root">
                  {/* The one primary action: accent disc (K11). */}
                  <button
                    ref={firstRef}
                    type="button"
                    className={`${rowClass} min-h-16 py-2`}
                    disabled={!activeBoardId}
                    data-testid="quick-new-job"
                    onClick={() => {
                      close();
                      setNewJobRequest((n) => n + 1);
                    }}
                  >
                    <span
                      className="shrink-0 w-10 h-10 -ml-1 rounded-full flex items-center justify-center"
                      style={{ background: C.accent, color: C.onAccent }}
                    >
                      <Play size={18} aria-hidden />
                    </span>
                    <span className="flex-1 min-w-0">
                      <span className="block text-base" style={{ color: C.textPrimary, fontWeight: 600 }}>
                        {t("quick.newJob")}
                      </span>
                      <span className="block text-sm truncate" style={{ color: C.textMuted }}>
                        {t("quick.newJobHint")}
                      </span>
                    </span>
                  </button>
                  <button
                    type="button"
                    className={`${rowClass} min-h-16 py-2`}
                    data-testid="quick-voice"
                    onClick={() => {
                      close();
                      voice.toggleButton();
                    }}
                  >
                    <RowBody icon={Mic} label={t("quick.voice")} hint={t("quick.voiceHint")} />
                  </button>
                  {lastChat && (
                    <Link
                      href={chatHref(lastChat.ref)}
                      onClick={close}
                      className={`${rowClass} min-h-16 py-2`}
                      data-testid="quick-last-chat"
                    >
                      <RowBody
                        icon={MessagesSquare}
                        label={t("quick.continueWith", { name: lastChat.name })}
                        hint={t("quick.lastChat")}
                      />
                    </Link>
                  )}

                  <div className="my-3" style={{ borderTop: `1px solid ${C.borderSubtle}` }} />

                  <button
                    ref={pagesRowRef}
                    type="button"
                    className={`${rowClass} min-h-12`}
                    data-testid="quick-pages"
                    aria-haspopup="true"
                    onClick={() => setQuickLevel("pages")}
                  >
                    <RowBody icon={LayoutGrid} label={t("quick.pages")} chevron />
                  </button>
                  <Link
                    href="/settings"
                    onClick={close}
                    className={`${rowClass} min-h-12`}
                    data-testid="quick-settings"
                  >
                    <RowBody icon={Settings} label={t("settings")} chevron />
                  </Link>
                </motion.div>
              ) : (
                <motion.div
                  key="pages"
                  {...levelMotion}
                  onPanEnd={onPagesPanEnd}
                  className="flex flex-col"
                  data-testid="quick-pages-level"
                >
                  <div className="flex items-center min-h-12 -ml-2">
                    <button
                      ref={backRef}
                      type="button"
                      onClick={() => setQuickLevel("root")}
                      aria-label={t("quick.back")}
                      className="flex items-center justify-center w-11 h-11 rounded-md cursor-pointer"
                      style={{ color: C.textSecondary }}
                    >
                      <ChevronLeft size={22} aria-hidden />
                    </button>
                    <h2 className="text-lg" style={{ color: C.textPrimary, fontWeight: 600 }}>
                      {t("quick.pages")}
                    </h2>
                  </div>
                  {pages.usedMost.length > 0 && (
                    <>
                      <SectionLabel>{t("quick.usedMost")}</SectionLabel>
                      {pages.usedMost.map(pageRow)}
                    </>
                  )}
                  {pages.others.length > 0 && (
                    <>
                      <SectionLabel>{t("quick.others")}</SectionLabel>
                      {pages.others.map(pageRow)}
                    </>
                  )}
                </motion.div>
              )}
            </motion.div>
          </>
        )}
      </AnimatePresence>

      {/* The New-task modal, opened by "New job". It brings its own head
          launcher (run as head now) and the "tonight" switch. */}
      <CreateTaskModal
        activeBoardId={activeBoardId}
        agents={boardAgents}
        hideTrigger
        openRequest={newJobRequest}
        onOpenTask={(id) => router.push(`/tasks?task=${encodeURIComponent(id)}`)}
      />
    </>
  );
}
