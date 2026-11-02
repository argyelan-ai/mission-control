"use client";

/**
 * Inbox tab "Between agents" (ADR-100 §4, stage 2d) — every session-to-
 * session conversation, read-only.
 *
 * Why it looks the way it does:
 * - It never counts toward the inbox badge: only "For you" asks something of
 *   the operator. A conversation between agents is something to read, so it
 *   stays quiet (§4: "zählt nicht fürs Badge, sonst wird es laut").
 * - A message here must never look like the operator's own. Every message
 *   sits left-aligned on a surface under its sender's address (the chat puts
 *   the operator's own words right-aligned in a bubble), and the opened
 *   conversation says once, on top, that this is information written between
 *   sessions — not an instruction, not from the operator (§6).
 * - The brake is the one thing that can need the operator. A paused
 *   conversation names it and links to "For you", where its approval card is
 *   decided — this tab itself has no buttons that change anything.
 */
import { useQuery } from "@tanstack/react-query";
import { useLocale, useTranslations } from "next-intl";
import { ChevronLeft, ChevronRight, Info, MessagesSquare } from "lucide-react";
import { api } from "@/lib/api";
import { C, STATUS_TEXT } from "@/lib/colors";
import { formatClockTime } from "@/lib/clockTime";
import { pairTitle, type CommsMessage, type Conversation } from "@/lib/comms";
import { Pill } from "@/components/shared/Pill";

/** Backend `comms_send.MAX_REPLIES` — the brake's number, shown in words. */
const BRAKE_AFTER = 6;
const POLL_MS = 30_000;

export function useConversations() {
  return useQuery({
    queryKey: ["comms-conversations"],
    queryFn: () => api.comms.conversations(),
    refetchInterval: POLL_MS,
    select: (d) => d.conversations,
  });
}

function BrakePill({ conv }: { conv: Conversation }) {
  const t = useTranslations("inbox");
  if (conv.state === "paused") {
    return (
      <Pill color={STATUS_TEXT.warning} casing="sentence" data-testid="conversation-brake">
        {t("brakeChip")}
      </Pill>
    );
  }
  if (conv.state === "stopped") {
    return (
      <Pill color={C.textMuted} casing="sentence" data-testid="conversation-stopped">
        {t("brakeStoppedChip")}
      </Pill>
    );
  }
  return null;
}

function ConversationRow({ conv, onOpen }: { conv: Conversation; onOpen: () => void }) {
  const t = useTranslations("inbox");
  const locale = useLocale();
  const time = formatClockTime(conv.last_message_at, locale);
  const last = conv.last_message;
  return (
    <button
      type="button"
      onClick={onOpen}
      data-testid="conversation-row"
      data-state={conv.state}
      className="w-full text-left rounded-lg px-4 py-3 flex items-start gap-3 cursor-pointer transition-colors hover:bg-[var(--color-bg-elevated)]"
      style={{ background: C.bgSurface, border: `1px solid ${C.border}` }}
    >
      <div className="flex-1 min-w-0">
        <div className="flex items-baseline gap-3">
          <span className="flex-1 min-w-0 truncate font-mono text-sm" style={{ color: C.textPrimary }}>
            {pairTitle(conv)}
          </span>
          {time && (
            <span className="shrink-0 text-xs tabular-nums" style={{ color: C.textMuted }}>
              {time}
            </span>
          )}
        </div>
        {last && (
          <p className="m-0 mt-1 text-sm line-clamp-2 break-words" dir="auto" style={{ color: C.textSecondary }}>
            <span className="font-mono text-xs" style={{ color: C.textMuted }}>
              {last.from ?? t("fromUnknown")}
            </span>{" "}
            {last.text}
          </p>
        )}
        <div className="flex flex-wrap items-center gap-2 mt-2">
          <Pill color={C.textMuted} casing="sentence">
            {t("messagesCount", { count: conv.message_count })}
          </Pill>
          <Pill color={C.textMuted} casing="sentence">
            {t("infoOnly")}
          </Pill>
          <BrakePill conv={conv} />
        </div>
      </div>
      <ChevronRight size={16} className="shrink-0 mt-1" style={{ color: C.textMuted }} aria-hidden />
    </button>
  );
}

function MessageBlock({ m }: { m: CommsMessage }) {
  const t = useTranslations("inbox");
  const locale = useLocale();
  const time = formatClockTime(m.created_at, locale);
  return (
    <li
      data-testid="conversation-message"
      className="rounded-lg px-4 py-3"
      style={{ background: C.bgSurface, border: `1px solid ${C.border}` }}
    >
      <div className="flex items-baseline gap-3">
        <span className="flex-1 min-w-0 truncate font-mono text-xs" style={{ color: C.textSecondary }}>
          {m.from ?? t("fromUnknown")}
        </span>
        {time && (
          <span className="shrink-0 text-xs tabular-nums" style={{ color: C.textMuted }}>
            {time}
          </span>
        )}
      </div>
      <p className="m-0 mt-1 text-sm whitespace-pre-wrap break-words" dir="auto" style={{ color: C.textPrimary }}>
        {m.text}
      </p>
    </li>
  );
}

function ConversationDetail({
  threadId,
  onBack,
  onGoForYou,
}: {
  threadId: string;
  onBack: () => void;
  onGoForYou: () => void;
}) {
  const t = useTranslations("inbox");
  const q = useQuery({
    queryKey: ["comms-messages", threadId],
    queryFn: () => api.comms.messages(threadId),
    refetchInterval: POLL_MS,
  });
  const conv = q.data?.conversation;

  return (
    <section data-testid="conversation-detail" className="flex flex-col gap-4">
      <button
        type="button"
        onClick={onBack}
        className="self-start -ml-1 flex items-center gap-1 min-h-11 bg-transparent border-0 p-0 cursor-pointer text-sm"
        style={{ color: C.textSecondary }}
      >
        <ChevronLeft size={18} aria-hidden />
        {t("backToConversations")}
      </button>

      {q.isError && (
        <p className="m-0 flex items-center gap-3 text-sm" style={{ color: C.textMuted }}>
          {t("conversationLoadFailed")}
          <button
            type="button"
            onClick={() => q.refetch()}
            className="min-h-11 bg-transparent border-0 p-0 cursor-pointer text-sm font-medium"
            style={{ color: C.textSecondary }}
          >
            {t("retry")}
          </button>
        </p>
      )}
      {q.isPending && (
        <p className="m-0 text-sm" style={{ color: C.textMuted }}>
          {t("loading")}
        </p>
      )}

      {conv && (
        <>
          <div className="flex flex-col gap-1">
            <h2 className="m-0 font-mono text-base font-semibold break-all" style={{ color: C.textPrimary }}>
              {pairTitle(conv)}
            </h2>
            {conv.state === "paused" ? (
              <p className="m-0 text-sm" style={{ color: STATUS_TEXT.warning }} data-testid="conversation-state">
                {t("brakePausedLine", { max: BRAKE_AFTER })}
              </p>
            ) : conv.state === "stopped" ? (
              <p className="m-0 text-sm" style={{ color: C.textMuted }} data-testid="conversation-state">
                {t("brakeStoppedLine")}
              </p>
            ) : (
              <p className="m-0 text-sm" style={{ color: C.textMuted }} data-testid="conversation-state">
                {t("messagesCount", { count: conv.message_count })}
              </p>
            )}
            {conv.state === "paused" && (
              <button
                type="button"
                onClick={onGoForYou}
                data-testid="conversation-decide"
                className="self-start flex items-center gap-1 min-h-11 bg-transparent border-0 p-0 cursor-pointer text-sm font-medium"
                style={{ color: C.textPrimary }}
              >
                {t("decideInForYou")}
                <ChevronRight size={16} aria-hidden />
              </button>
            )}
          </div>

          <p
            data-testid="conversation-info-only"
            className="m-0 flex items-start gap-2 text-xs"
            style={{ color: C.textMuted }}
          >
            <Info size={14} className="shrink-0 mt-px" aria-hidden />
            <span>{t("notFromYou")}</span>
          </p>

          <ol className="m-0 p-0 list-none flex flex-col gap-2">
            {q.data!.messages.map((m) => (
              <MessageBlock key={m.id} m={m} />
            ))}
          </ol>
        </>
      )}
    </section>
  );
}

export function BetweenAgents({
  openThreadId,
  onOpen,
  onGoForYou,
}: {
  openThreadId: string | null;
  onOpen: (threadId: string | null) => void;
  onGoForYou: () => void;
}) {
  const t = useTranslations("inbox");
  const q = useConversations();

  if (openThreadId) {
    return <ConversationDetail threadId={openThreadId} onBack={() => onOpen(null)} onGoForYou={onGoForYou} />;
  }

  if (q.isError) {
    return (
      <p className="m-0 flex items-center gap-3 text-sm" style={{ color: C.textMuted }}>
        {t("conversationsLoadFailed")}
        <button
          type="button"
          onClick={() => q.refetch()}
          className="min-h-11 bg-transparent border-0 p-0 cursor-pointer text-sm font-medium"
          style={{ color: C.textSecondary }}
        >
          {t("retry")}
        </button>
      </p>
    );
  }
  if (q.isPending) {
    return (
      <p className="m-0 text-sm" style={{ color: C.textMuted }}>
        {t("loading")}
      </p>
    );
  }
  if (q.data.length === 0) {
    return (
      <div data-testid="between-empty" className="flex flex-col items-center text-center gap-3 py-16 px-4">
        <MessagesSquare size={24} style={{ color: C.textMuted }} aria-hidden />
        <p className="m-0 text-sm font-medium" style={{ color: C.textSecondary }}>
          {t("betweenEmpty")}
        </p>
        <p className="m-0 text-sm max-w-sm" style={{ color: C.textMuted }}>
          {t("betweenEmptyHint")}
        </p>
      </div>
    );
  }
  return (
    <div className="flex flex-col gap-2" data-testid="conversation-list">
      {q.data.map((c) => (
        <ConversationRow key={c.thread_id} conv={c} onOpen={() => onOpen(c.thread_id)} />
      ))}
    </div>
  );
}
