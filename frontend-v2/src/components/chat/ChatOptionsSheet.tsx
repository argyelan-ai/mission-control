"use client";

/**
 * ChatOptionsSheet — everything the desktop header keeps in its toolbar,
 * folded into one bottom sheet for the mobile chat screen.
 *
 * Why a sheet and not a toolbar: on a phone the chat screen has room for the
 * conversation and nothing else. The desktop's two segmented switchers plus
 * the panel rail would eat a third of the viewport, so they move behind the
 * one affordance a phone user expects for "the rest of this screen's
 * options" — and they arrive in the thumb zone, not under the notch.
 *
 * Owns no state of its own: the caller holds view/detail/panel state (it is
 * persisted per-agent in localStorage by sessions/page.tsx) and this only
 * reports intent.
 */
import { AnimatePresence, motion } from "framer-motion";
import { Check, GitCompare, Globe, MonitorPlay, MessagesSquare, X } from "lucide-react";
import { C } from "@/lib/colors";
import { CENTER_VIEWS, DETAIL_LEVELS, type CenterView, type DetailLevel } from "./chatOptions";
import type { PanelKind } from "./PanelRail";

const VIEW_ICON: Record<CenterView, typeof MonitorPlay> = {
  chat: MessagesSquare,
  terminal: MonitorPlay,
};

const PANELS: { key: PanelKind; label: string; icon: typeof GitCompare }[] = [
  { key: "diff", label: "Diff", icon: GitCompare },
  { key: "browser", label: "Browser", icon: Globe },
];

interface ChatOptionsSheetProps {
  open: boolean;
  onClose: () => void;
  /** The view actually in effect (already narrowed by `canChat`). */
  centerView: CenterView;
  onCenterViewChange: (view: CenterView) => void;
  /** False for agents with no transcript — the Chat row is then unreachable. */
  canChat: boolean;
  detailLevel: DetailLevel;
  onDetailLevelChange: (level: DetailLevel) => void;
  /** Omitted when the caller has no side panels to offer. */
  onOpenPanel?: (panel: PanelKind) => void;
}

function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <div className="label-sys px-1 pb-2 pt-4" style={{ color: C.textMuted }}>
      {children}
    </div>
  );
}

export function ChatOptionsSheet({
  open,
  onClose,
  centerView,
  onCenterViewChange,
  canChat,
  detailLevel,
  onDetailLevelChange,
  onOpenPanel,
}: ChatOptionsSheetProps) {
  return (
    <AnimatePresence>
      {open && (
        <>
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.18 }}
            // Top edge at the app bar, not at 0: this lives inside the content
            // column's z-10 stacking context and can never paint over the
            // z-40 header (see --mobile-chat-topbar-h). Stopping there is honest;
            // pretending to cover it would just leave a bright strip.
            className="fixed inset-x-0 bottom-0 top-[var(--mobile-chat-topbar-h)] z-40 md:hidden"
            // Scrim derived from the palette's deepest neutral (bg-deep at
            // 75%), not the warm near-black the older MobileNav drawer uses —
            // v4 off-blacks are neutral by doctrine.
            style={{ background: "rgba(10,10,10,0.75)" }}
            onClick={onClose}
            aria-hidden="true"
          />
          <motion.div
            role="dialog"
            aria-modal="true"
            aria-label="Chat-Optionen"
            data-testid="chat-options-sheet"
            initial={{ y: "100%" }}
            animate={{ y: 0 }}
            exit={{ y: "100%" }}
            transition={{ duration: 0.28, ease: [0.16, 1, 0.3, 1] }}
            className="fixed inset-x-0 bottom-0 z-50 md:hidden px-3 pb-safe"
            style={{
              background: C.bgElevated,
              borderTopLeftRadius: "var(--radius-xl)",
              borderTopRightRadius: "var(--radius-xl)",
              // The 2px accent edge is this system's "this is an overlay" mark.
              borderTop: `2px solid ${C.accent}`,
              boxShadow: "var(--shadow-elevated)",
            }}
          >
            <div className="flex items-center justify-between pt-3 pb-1">
              <span className="text-[16px] font-semibold" style={{ color: C.textPrimary }}>
                Optionen
              </span>
              <button
                type="button"
                onClick={onClose}
                aria-label="Optionen schliessen"
                className="flex items-center justify-center w-10 h-10 rounded-lg cursor-pointer"
                style={{ color: C.textMuted }}
              >
                <X size={17} />
              </button>
            </div>

            <SectionLabel>Ansicht</SectionLabel>
            <div role="radiogroup" aria-label="Ansicht" className="flex flex-col">
              {CENTER_VIEWS.map(({ key, label }) => {
                const Icon = VIEW_ICON[key];
                const active = centerView === key;
                const disabled = key === "chat" && !canChat;
                return (
                  <button
                    key={key}
                    type="button"
                    role="radio"
                    aria-checked={active}
                    disabled={disabled}
                    title={disabled ? "Kein Transkript verfügbar" : undefined}
                    onClick={() => {
                      onCenterViewChange(key);
                      onClose();
                    }}
                    className="flex items-center gap-3 min-h-touch px-2 rounded-lg text-left cursor-pointer disabled:cursor-not-allowed disabled:opacity-40"
                    style={{ color: active ? C.textPrimary : C.textSecondary }}
                  >
                    <Icon size={16} style={{ color: active ? C.accent : C.textMuted }} aria-hidden="true" />
                    <span className="flex-1 text-[14px]">{label}</span>
                    {active && <Check size={15} style={{ color: C.accent }} aria-hidden="true" />}
                  </button>
                );
              })}
            </div>

            {onOpenPanel && (
              <>
                <SectionLabel>Panels</SectionLabel>
                <div className="flex flex-col">
                  {PANELS.map(({ key, label, icon: Icon }) => (
                    <button
                      key={key}
                      type="button"
                      onClick={() => {
                        onOpenPanel(key);
                        onClose();
                      }}
                      className="flex items-center gap-3 min-h-touch px-2 rounded-lg text-left cursor-pointer"
                      style={{ color: C.textSecondary }}
                    >
                      <Icon size={16} style={{ color: C.textMuted }} aria-hidden="true" />
                      <span className="flex-1 text-[14px]">{label}</span>
                    </button>
                  ))}
                </div>
              </>
            )}

            {centerView === "chat" && (
              <>
                <SectionLabel>Detailgrad</SectionLabel>
                <div
                  role="radiogroup"
                  aria-label="Detailgrad"
                  className="flex items-center rounded-lg overflow-hidden mb-3"
                  style={{ border: `1px solid ${C.border}` }}
                >
                  {DETAIL_LEVELS.map(({ key, label }, i) => (
                    <button
                      key={key}
                      type="button"
                      role="radio"
                      aria-checked={detailLevel === key}
                      onClick={() => onDetailLevelChange(key)}
                      className="flex-1 min-h-touch text-[13px] font-medium cursor-pointer transition-colors"
                  data-testid={`sheet-detail-${key}`}
                      style={{
                        background: detailLevel === key ? C.accentSubtle : "transparent",
                        color: detailLevel === key ? C.accent : C.textSecondary,
                        borderLeft: i > 0 ? `1px solid ${C.border}` : undefined,
                      }}
                    >
                      {label}
                    </button>
                  ))}
                </div>
              </>
            )}
          </motion.div>
        </>
      )}
    </AnimatePresence>
  );
}
