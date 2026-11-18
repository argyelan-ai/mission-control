"use client";

/**
 * ChatChangesInline — one quiet row under the agent's latest answer:
 * "3 files changed in this chat +71 −2 · Open diff". Tap → the Changes panel,
 * on its "This chat" tab.
 *
 * Same data as that tab (`GET /agents/{id}/chat/changes`, same query key, so
 * the two share one cache). The endpoint only knows the whole chat — a file's
 * +/− is its net change over every edit — so the row says "in this chat" and
 * stands under the latest answer only; per-turn numbers would need a backend
 * change. Shown once the turn is over (while the agent works the list is
 * still moving) and only when a file really changed (K3).
 */
import { useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { ChevronRight, FilePen } from "lucide-react";
import { api } from "@/lib/api";
import { C } from "@/lib/colors";
import { Counts } from "./ChatChangesList";
import { saveTab } from "./DiffPanel";
import { TIMELINE_CARD_GUTTER } from "./timelineCard";

export function ChatChangesInline({
  agentId,
  working,
  onOpen,
}: {
  agentId: string;
  /** The agent's turn is still running — nothing is shown or fetched. */
  working: boolean;
  /** Opens the Changes panel. */
  onOpen: () => void;
}) {
  const t = useTranslations("sessions.diff");
  const query = useQuery({
    queryKey: ["chat-changes", agentId],
    queryFn: () => api.chat.changes(agentId),
    enabled: !working,
  });
  const stats = query.data?.stats;
  if (working || query.isError || !stats || stats.files === 0) return null;

  return (
    <div className={TIMELINE_CARD_GUTTER}>
      <button
        type="button"
        data-testid="chat-changes-inline"
        onClick={() => {
          saveTab("chat");
          onOpen();
        }}
        className="flex w-full items-center gap-2 rounded-lg px-3 text-left min-h-11 md:min-h-[34px] cursor-pointer bg-transparent transition-colors hover:bg-[var(--color-bg-hover)]"
        style={{ border: `1px solid ${C.border}` }}
      >
        <FilePen size={13} className="shrink-0" style={{ color: C.textMuted }} aria-hidden="true" />
        <span className="min-w-0 truncate text-sm" style={{ color: C.textSecondary }}>
          {t("inlineFiles", { count: stats.files })}
        </span>
        <Counts file={stats} />
        <span className="ml-auto shrink-0 flex items-center gap-1 text-sm" style={{ color: C.textMuted }}>
          {/* The word gives way on a phone (operator pictures 18.11.2026:
              "1 Datei in diesem Chat geä…" at 390 px) — the row itself is
              the button, the chevron says where it goes. */}
          <span data-testid="chat-changes-open-word" className="hidden sm:inline">{t("inlineOpen")}</span>
          <ChevronRight size={13} aria-hidden="true" />
        </span>
      </button>
    </div>
  );
}
