"use client";

/**
 * GroupChatView — der Gruppenraum in der Mitte der Sessions-Seite (ADR-075).
 *
 * Bewusst kein zweiter ChatView: dessen Reducer verarbeitet Transkript-Events
 * EINES CLI-Agenten. Hier fliessen comm_v2-Nachrichten mehrerer Absender über
 * `useGroupStream`. Übernommen ist dagegen die Haltung des Sessions-Chats —
 * eine wahrhaftige Statuszeile, Mitlaufen nur solange der Leser unten steht,
 * und ein Kopf, der auf dem Handy genauso funktioniert wie am Schreibtisch.
 *
 * Es gibt keinen Modus-Schalter: läuft keine Runde, ist die Gruppe ein
 * Chatraum (Erwähnte antworten direkt); läuft eine, fliesst Marks Einwurf in
 * den nächsten Runden-Brief. Das Verhalten folgt dem Status, nicht einem Knopf.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { ChevronLeft, FileText, Pause, Play, Square, Users } from "lucide-react";
import { api } from "@/lib/api";
import { C } from "@/lib/colors";
import { notify } from "@/lib/notify";
import { useGroupStream } from "@/hooks/useGroupStream";
import type { GroupDetail, GroupRoundInfo } from "@/lib/groupTypes";
import { AvatarStack } from "@/components/groupchat/AvatarStack";
import { GroupComposer } from "@/components/groupchat/GroupComposer";
import { GroupGateCard } from "@/components/groupchat/GroupGateCard";
import { GroupMessage } from "@/components/groupchat/GroupMessage";
import { GroupStatusLine } from "@/components/groupchat/GroupStatusLine";
import { RoundDivider } from "@/components/groupchat/RoundDivider";
import { ConfirmDialog } from "@/components/shared/ConfirmDialog";

interface GroupChatViewProps {
  group: GroupDetail;
  onBack?: () => void;
  onGroupChanged: (group: GroupDetail) => void;
  onOpenResult?: () => void;
}

export function GroupChatView({
  group,
  onBack,
  onGroupChanged,
  onOpenResult,
}: GroupChatViewProps) {
  const t = useTranslations("sessions.groups");
  const stream = useGroupStream(group.id);
  const [rounds, setRounds] = useState<GroupRoundInfo[]>([]);
  const [busy, setBusy] = useState(false);
  const [confirmStop, setConfirmStop] = useState(false);

  const scrollRef = useRef<HTMLDivElement>(null);
  const stickToBottom = useRef(true);

  // Runden dienen hier nur als Trenner-Anker (brief_seq). Neu geladen wird,
  // sobald sich die Rundenzahl ändert — nicht im Sekundentakt.
  useEffect(() => {
    let cancelled = false;
    api.groups
      .rounds(group.id)
      .then((res) => !cancelled && setRounds(res.rounds))
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [group.id, group.current_round_no, group.status]);

  // Mitlaufen nur, solange der Leser wirklich unten steht — sonst reisst ein
  // eintreffender Beitrag ihn aus der Stelle, die er gerade liest (teuer
  // bezahlte Lektion aus dem Sessions-Chat).
  const onScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    stickToBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 120;
  }, []);

  useEffect(() => {
    const el = scrollRef.current;
    if (el && stickToBottom.current) el.scrollTop = el.scrollHeight;
  }, [stream.messages]);

  const roundBySeq = useMemo(() => {
    const map = new Map<number, number>();
    for (const r of rounds) {
      if (r.brief_seq != null) map.set(r.brief_seq, r.round_no);
    }
    return map;
  }, [rounds]);

  const memberById = useMemo(() => {
    const map = new Map<string, { name: string; emoji: string | null }>();
    for (const m of group.members) map.set(m.id, { name: m.name, emoji: m.emoji });
    return map;
  }, [group.members]);

  const composerMembers = useMemo(
    () =>
      group.members
        .filter((m) => !m.archived && m.slug)
        .map((m) => ({ slug: m.slug as string, name: m.name, emoji: m.emoji })),
    [group.members],
  );

  const control = useCallback(
    async (action: "start" | "pause" | "stop") => {
      setBusy(true);
      try {
        const updated = await api.groups[action](group.id);
        onGroupChanged(updated);
        notify.success(
          action === "start" ? t("started") : action === "pause" ? t("paused") : t("stopped"),
        );
      } catch {
        notify.error(t("actionFailed"));
      } finally {
        setBusy(false);
      }
    },
    [group.id, onGroupChanged, t],
  );

  const handleSend = useCallback(
    async (text: string) => {
      try {
        await stream.send(text);
        stickToBottom.current = true;
      } catch {
        notify.error(t("actionFailed"));
      }
    },
    [stream, t],
  );

  const running = group.status === "running";
  const canStart = ["draft", "idle", "paused", "waiting_gate"].includes(group.status);
  const gateQuestion =
    group.status === "waiting_gate" ? stream.state.gateQuestion ?? group.goal : null;

  return (
    // `flex-1 min-h-0 overflow-hidden` wie ChatView — NICHT `h-full`: sonst
    // wächst die Spalte über die Bildschirmhöhe hinaus, die ganze Seite
    // scrollt statt nur des Verlaufs, und der Kopf wandert auf dem iPhone
    // unter die Statusleiste (Operator-Befund 21.08.2026, iPhone 15).
    <div
      className="flex flex-col flex-1 min-h-0 overflow-hidden"
      data-testid="group-chat-view"
    >
      {/* Kopf — mobil mit Zurück-Chevron, Titel mittig (Muster aus dem
          Handy-Chat), darunter das Ziel als Kontextzeile: eine Gruppe ohne
          sichtbares Ziel wäre nur ein Haufen Agenten. */}
      {/* pt-safe-top wie in ChatView: auf dem Handy laeuft die Sessions-Seite
          chromelos (AppShell `mobileChromeless`) — ueber dieser Zeile liegt
          nichts mehr, also muss SIE die Statusleiste abfedern. Ohne das sass
          der Gruppen-Kopf unter der Uhrzeit (Operator-Befund 21.08.2026). */}
      <div
        className="shrink-0 flex items-center gap-2 px-3 py-2 pt-safe-top md:pt-2 border-b"
        style={{ borderColor: C.border }}
      >
        {onBack && (
          <button
            type="button"
            onClick={onBack}
            aria-label={t("sectionTitle")}
            className="md:hidden flex items-center justify-center w-9 h-9 rounded-lg cursor-pointer"
            style={{ color: C.textMuted }}
          >
            <ChevronLeft size={18} />
          </button>
        )}
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 min-w-0">
            <AvatarStack
              members={group.members.map((m) => ({ id: m.id, emoji: m.emoji, name: m.name }))}
            />
            <span className="truncate text-[14px] font-semibold" style={{ color: C.textPrimary }}>
              {group.name}
            </span>
          </div>
          <div className="truncate text-[12px]" style={{ color: C.textMuted }}>
            <span className="label-sys mr-1">{t("goalLabel")}</span>
            {group.goal}
          </div>
        </div>
        <div className="flex items-center gap-1 shrink-0">
          {canStart && (
            <button
              type="button"
              onClick={() => control("start")}
              disabled={busy}
              aria-label={group.status === "paused" ? t("resume") : t("start")}
              title={group.status === "paused" ? t("resume") : t("start")}
              className="flex items-center justify-center w-9 h-9 rounded-lg cursor-pointer disabled:opacity-40"
              style={{ color: C.accent }}
            >
              <Play size={16} />
            </button>
          )}
          {running && (
            <button
              type="button"
              onClick={() => control("pause")}
              disabled={busy}
              aria-label={t("pause")}
              title={t("pause")}
              className="flex items-center justify-center w-9 h-9 rounded-lg cursor-pointer disabled:opacity-40"
              style={{ color: C.textMuted }}
            >
              <Pause size={16} />
            </button>
          )}
          {group.status !== "done" && (
            <button
              type="button"
              onClick={() => setConfirmStop(true)}
              disabled={busy}
              aria-label={t("stop")}
              title={t("stop")}
              className="flex items-center justify-center w-9 h-9 rounded-lg cursor-pointer disabled:opacity-40"
              style={{ color: C.textMuted }}
            >
              <Square size={15} />
            </button>
          )}
          {onOpenResult && (
            <button
              type="button"
              onClick={onOpenResult}
              aria-label={t("resultPanel")}
              title={t("resultPanel")}
              className="relative flex items-center justify-center w-9 h-9 rounded-lg cursor-pointer"
              style={{ color: C.textMuted }}
            >
              <FileText size={16} />
              {stream.docVersion != null && (
                <span
                  className="absolute top-1.5 right-1.5 w-1.5 h-1.5 rounded-full"
                  style={{ background: C.accent }}
                />
              )}
            </button>
          )}
        </div>
      </div>

      {/* Verlauf */}
      <div
        ref={scrollRef}
        onScroll={onScroll}
        className="flex-1 min-h-0 overflow-y-auto px-3 py-3 space-y-2"
        data-testid="group-messages"
      >
        {stream.messages.length === 0 && !stream.loading && (
          <div className="h-full flex flex-col items-center justify-center gap-3 text-center px-6">
            <Users size={20} style={{ color: C.textDim }} />
            <p className="text-[13px] max-w-[42ch]" style={{ color: C.textMuted }}>
              {t("emptyRoom")}
            </p>
            {canStart && (
              <button
                type="button"
                onClick={() => control("start")}
                disabled={busy}
                className="px-3 py-1.5 rounded-lg text-[12px] font-medium cursor-pointer disabled:opacity-40"
                style={{ background: C.accentSubtle, color: C.accent, border: `1px solid ${C.borderAccent}` }}
              >
                {t("startFirstRound")}
              </button>
            )}
          </div>
        )}
        {stream.messages.map((message, index) => {
          const previous = stream.messages[index - 1];
          const sender = message.sender_id ? memberById.get(message.sender_id) : undefined;
          const roundNo = roundBySeq.get(message.seq);
          const groupWithPrevious =
            previous != null &&
            previous.sender_type === message.sender_type &&
            previous.sender_id === message.sender_id &&
            roundNo == null;
          return (
            <div key={message.id}>
              {roundNo != null && <RoundDivider round={roundNo} time={message.created_at} />}
              <GroupMessage
                message={message}
                senderName={sender?.name ?? null}
                senderEmoji={sender?.emoji ?? null}
                isOwn={message.sender_type === "user"}
                groupWithPrevious={groupWithPrevious}
              />
            </div>
          );
        })}
      </div>

      {/* Gate + Statuszeile + Composer */}
      <div className="shrink-0 px-3 pb-3 space-y-2">
        {gateQuestion && (
          <GroupGateCard
            question={gateQuestion}
            busy={busy}
            onApprove={() => control("start")}
            onReject={() => control("pause")}
          />
        )}
        <GroupStatusLine
          status={group.status}
          state={stream.state}
          connected={stream.connected}
          budgetUsd={group.budget_usd}
          spentUsd={stream.state.lastRoundCostUsd}
        />
        <GroupComposer
          members={composerMembers}
          onSend={handleSend}
          sending={stream.sending}
          roundRunning={running}
        />
      </div>

      {confirmStop && (
        <ConfirmDialog
          open={confirmStop}
          title={t("stopConfirmTitle")}
          body={
            group.lifecycle === "standing"
              ? t("stopConfirmStanding")
              : t("stopConfirmOneShot")
          }
          confirmLabel={t("stop")}
          // Beenden ist reversibel (Dauergruppe) bzw. sauberer Abschluss
          // (Auftrag) — kein roter Not-Aus-Knopf.
          danger={false}
          onConfirm={() => {
            setConfirmStop(false);
            control("stop");
          }}
          onCancel={() => setConfirmStop(false)}
        />
      )}
    </div>
  );
}
