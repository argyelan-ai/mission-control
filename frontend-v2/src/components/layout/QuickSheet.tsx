"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { useQuery } from "@tanstack/react-query";
import { AnimatePresence, motion, useReducedMotion, type PanInfo } from "framer-motion";
import { MessagesSquare, Mic, Play, X } from "lucide-react";
import { api } from "@/lib/api";
import { useAppStore } from "@/lib/store";
import { C, alpha } from "@/lib/colors";
import { AREA_TILES } from "@/lib/mobileNav";
import { chatHref, type ChatRef } from "@/lib/recentChats";
import { CreateTaskModal } from "@/components/shared/CreateTaskModal";
import { useVoiceContext } from "@/components/voice/VoiceWidget";
import { useMobileNav } from "./MobileNav";
import { useRecentChats } from "./MobileTabBar";

/** How many "continue a chat" chips fit one row at 393 px. */
const CHIP_MAX = 3;

type Named = { id: string; name: string };

/**
 * Resolve the device's recent chats to names. A chat whose agent or group is
 * gone (deleted, renamed id) is skipped instead of shown as a raw id.
 */
export function resolveRecentChats(
  recent: ChatRef[],
  agents: Named[],
  groups: Named[],
  max = CHIP_MAX,
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

function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <div className="text-sm px-1 pt-4 pb-2" style={{ color: C.textMuted }}>
      {children}
    </div>
  );
}

/**
 * QuickSheet — what the ⊕ in the phone tab bar opens (mobile nav V2):
 *
 *   New job          → the New-task modal (run as head now or tonight)
 *   Voice command    → the voice assistant
 *   Continue a chat  → the last chats of this device
 *   All areas        → Runtimes · Insights · Agents · Memory · Schedule · More…
 *
 * "More…" opens the full menu drawer (every route, board, account) — that is
 * why the bar needs no "More" tab. A sheet, not a page (HIG): it rises from
 * the bottom into thumb reach and swipes down to close. Animated with
 * transform + opacity only; reduced motion → no slide.
 */
export function QuickSheet() {
  const t = useTranslations("nav");
  const router = useRouter();
  const reduce = useReducedMotion();
  const { quickOpen, setQuickOpen, setOpen: setMenuOpen } = useMobileNav();
  const { activeBoardId } = useAppStore();
  const voice = useVoiceContext();
  const recent = useRecentChats();
  const [newJobRequest, setNewJobRequest] = useState(0);
  const firstRef = useRef<HTMLButtonElement>(null);

  // Names for the chips — the same queries (and cache) as the chats page.
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

  const chips = resolveRecentChats(recent, [...dockerAgents, ...hostAgents], groups);

  useEffect(() => {
    if (!quickOpen) return;
    const id = window.setTimeout(() => firstRef.current?.focus(), 50);
    return () => window.clearTimeout(id);
  }, [quickOpen]);

  const close = () => setQuickOpen(false);

  function onDragEnd(_: unknown, info: PanInfo) {
    if (info.offset.y > 80 || info.velocity.y > 600) close();
  }

  const rowClass =
    "w-full flex items-center gap-4 min-h-14 px-1 py-2 rounded-lg text-left cursor-pointer transition-colors hover:bg-[var(--color-bg-hover)]";

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
              aria-label={t("quick.title")}
              data-testid="quick-sheet"
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
              {/* Grabber — tells the thumb it can pull the sheet down. */}
              <div className="flex justify-center pt-2" aria-hidden>
                <span className="w-10 h-1 rounded-full" style={{ background: C.borderActive }} />
              </div>
              <div className="flex items-center justify-between pt-2">
                <h2 className="text-lg" style={{ color: C.textPrimary, fontWeight: 600 }}>
                  {t("quick.title")}
                </h2>
                <button
                  type="button"
                  onClick={close}
                  aria-label={t("quick.close")}
                  className="flex items-center justify-center w-11 h-11 rounded-md cursor-pointer -mr-2"
                  style={{ color: C.textSecondary }}
                >
                  <X size={20} aria-hidden />
                </button>
              </div>

              <div className="flex flex-col">
                <button
                  ref={firstRef}
                  type="button"
                  className={rowClass}
                  disabled={!activeBoardId}
                  data-testid="quick-new-job"
                  onClick={() => {
                    close();
                    setNewJobRequest((n) => n + 1);
                  }}
                >
                  <Play size={20} aria-hidden style={{ color: C.textSecondary }} className="shrink-0" />
                  <span className="min-w-0">
                    <span className="block text-base" style={{ color: C.textPrimary, fontWeight: 500 }}>
                      {t("quick.newJob")}
                    </span>
                    <span className="block text-sm" style={{ color: C.textMuted }}>
                      {t("quick.newJobHint")}
                    </span>
                  </span>
                </button>
                <button
                  type="button"
                  className={rowClass}
                  data-testid="quick-voice"
                  onClick={() => {
                    close();
                    voice.toggleButton();
                  }}
                >
                  <Mic size={20} aria-hidden style={{ color: C.textSecondary }} className="shrink-0" />
                  <span className="min-w-0">
                    <span className="block text-base" style={{ color: C.textPrimary, fontWeight: 500 }}>
                      {t("quick.voice")}
                    </span>
                    <span className="block text-sm" style={{ color: C.textMuted }}>
                      {t("quick.voiceHint")}
                    </span>
                  </span>
                </button>
              </div>

              {chips.length > 0 && (
                <>
                  <SectionLabel>{t("quick.continueChat")}</SectionLabel>
                  <div className="flex flex-wrap gap-2" data-testid="quick-chats">
                    {chips.map(({ ref, name }) => (
                      <Link
                        key={`${ref.kind}:${ref.id}`}
                        href={chatHref(ref)}
                        onClick={close}
                        className="inline-flex items-center gap-2 h-11 px-4 rounded-full text-sm cursor-pointer max-w-full"
                        style={{ color: C.textPrimary, border: `1px solid ${C.borderActive}` }}
                      >
                        <MessagesSquare size={16} aria-hidden className="shrink-0" style={{ color: C.textSecondary }} />
                        <span className="truncate">{name}</span>
                      </Link>
                    ))}
                  </div>
                </>
              )}

              <SectionLabel>{t("quick.allAreas")}</SectionLabel>
              <div className="grid grid-cols-3 gap-2" data-testid="quick-areas">
                {AREA_TILES.map(({ key, href, icon: Icon, labelKey }) => {
                  const inner = (
                    <>
                      <Icon size={20} aria-hidden style={{ color: C.textSecondary }} />
                      <span className="text-sm truncate max-w-full" style={{ color: C.textPrimary }}>
                        {t(labelKey)}
                      </span>
                    </>
                  );
                  const tileClass =
                    "flex flex-col items-center justify-center gap-2 min-h-16 px-2 py-3 rounded-lg cursor-pointer transition-colors hover:bg-[var(--color-bg-hover)]";
                  const tileStyle = { background: C.bgSurface };
                  return href ? (
                    <Link key={key} href={href} onClick={close} className={tileClass} style={tileStyle} data-area={key}>
                      {inner}
                    </Link>
                  ) : (
                    <button
                      key={key}
                      type="button"
                      className={tileClass}
                      style={tileStyle}
                      data-area={key}
                      onClick={() => {
                        close();
                        setMenuOpen(true);
                      }}
                    >
                      {inner}
                    </button>
                  );
                })}
              </div>
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
