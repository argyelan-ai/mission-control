"use client";

import { useTranslations } from "next-intl";
import { ChevronLeft, MessageSquareReply, type LucideIcon } from "lucide-react";
import { C } from "@/lib/colors";
import { useKeyboardOpen } from "@/hooks/useKeyboardOpen";

export type DetailMainAction = {
  label: string;
  icon?: LucideIcon;
  onClick: () => void;
};

/** One quiet slot: icon over a word, 44 px target (K11: secondary = no frame). */
export const BAR_SLOT =
  "flex flex-col items-center justify-center gap-1 min-w-14 min-h-12 px-2 rounded-md text-xs cursor-pointer transition-colors hover:bg-[var(--color-bg-hover)]";

/**
 * DetailActionBar — the phone's action bar at the bottom of a detail screen
 * (mobile nav V2, "Baustein für alle drei"):
 *
 *   ‹ Back · [ main action ] · Reply · ⋯ More
 *
 * Why at the bottom: on iPhone 15 and newer the top corners sit 127–144 mm
 * above the bottom edge — out of thumb reach, one-handed. The title row on
 * top stays for orientation. The main action mirrors the screen's one primary
 * button (K11), it does not invent a second one. The bar steps aside while
 * the on-screen keyboard is up (the input belongs right above it) and keeps
 * the home indicator strip free. Desktop: hidden (`md:hidden`).
 *
 * `more` is a node so each screen can bring its own menu (task ⋯ menu, chat
 * options sheet) — rendered as the last slot.
 */
export function DetailActionBar({
  onBack,
  main,
  onReply,
  more,
  testId = "detail-action-bar",
}: {
  onBack: () => void;
  main?: DetailMainAction | null;
  onReply?: () => void;
  more?: React.ReactNode;
  testId?: string;
}) {
  const t = useTranslations("nav.detailBar");
  const keyboardOpen = useKeyboardOpen();
  if (keyboardOpen) return null;
  const MainIcon = main?.icon;

  return (
    <div
      role="toolbar"
      aria-label={t("label")}
      data-testid={testId}
      className="md:hidden shrink-0 flex items-center gap-2 px-2 pt-2"
      style={{
        background: "var(--detail-bg, var(--color-bg-surface))",
        borderTop: `1px solid ${C.border}`,
        paddingBottom: "max(env(safe-area-inset-bottom), 0.5rem)",
      }}
    >
      <button type="button" onClick={onBack} className={BAR_SLOT} style={{ color: C.textSecondary }}>
        <ChevronLeft size={20} aria-hidden />
        <span>{t("back")}</span>
      </button>

      {main ? (
        <button
          type="button"
          onClick={main.onClick}
          data-testid="detail-main-action"
          className="flex-1 min-w-0 inline-flex items-center justify-center gap-2 h-12 px-4 rounded-md text-base cursor-pointer"
          style={{ background: C.accent, color: C.onAccent, fontWeight: 600 }}
        >
          {MainIcon && <MainIcon size={18} aria-hidden className="shrink-0" />}
          <span className="truncate">{main.label}</span>
        </button>
      ) : (
        <span className="flex-1" aria-hidden />
      )}

      {onReply && (
        <button type="button" onClick={onReply} className={BAR_SLOT} style={{ color: C.textSecondary }}>
          <MessageSquareReply size={20} aria-hidden />
          <span>{t("reply")}</span>
        </button>
      )}
      {more}
    </div>
  );
}
