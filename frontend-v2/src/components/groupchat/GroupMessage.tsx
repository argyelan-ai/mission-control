"use client";

/**
 * GroupMessage — eine Nachricht im Gruppen-Thread (ADR-075).
 *
 * Drei Register statt Sprecherfarben: Marks Beitrag ist eine rechtsbündige
 * Blase, ein Agentenbeitrag steht flach links mit Avatar + Name, System-Zeilen
 * (Runden-Briefe, Gate-Notizen) laufen zentriert und leise in der Mitte.
 * Die Unterscheidung trägt bewusst über Form und Position — die Palette bleibt
 * achromatisch, weil Farbe im Leitstand Zustand bedeutet und nicht Identität;
 * bei zehn Sprechern wäre eine Farbskala ohnehin nur noch Dekoration.
 *
 * Erwähnungen (`message.mentions`) werden NICHT zusätzlich gerendert: sie
 * stehen bereits im Text, und der angezeigte Text wird nie umgeschrieben.
 */
import { useTranslations } from "next-intl";
import { C } from "@/lib/colors";
import { EntityIcon } from "@/components/shared/EntityIcon";
import { MarkdownContent } from "@/components/chat/MarkdownContent";
import type { GroupMessage as GroupMessageData } from "@/lib/groupTypes";

interface GroupMessageProps {
  message: GroupMessageData;
  /** Aufgelöster Anzeigename des Absenders; null = unbekannt → dann zeigen wir
   *  keinen Namen statt eine UUID oder einen erfundenen Platzhalter. */
  senderName: string | null;
  senderEmoji: string | null;
  isOwn: boolean;
  /** Vorgänger kommt vom selben Sprecher → Kopfzeile weglassen, damit ein
   *  mehrteiliger Beitrag als ein Block liest statt als Stakkato. */
  groupWithPrevious?: boolean;
}

/** HH:MM, 24h, de-CH. Ein kaputter Zeitstempel darf die Kopfzeile nicht
 *  killen — dann bleibt die Uhrzeit weg statt „Invalid Date" zu zeigen. */
function formatClock(raw: string | null | undefined): string | null {
  if (!raw) return null;
  const d = new Date(raw);
  if (Number.isNaN(d.getTime())) return null;
  return d.toLocaleTimeString("de-CH", { hour: "2-digit", minute: "2-digit", hour12: false });
}

export function GroupMessage({
  message,
  senderName,
  senderEmoji,
  isOwn,
  groupWithPrevious = false,
}: GroupMessageProps) {
  const t = useTranslations("sessions.groups");

  const pending = message.pending === true;
  // Gedimmt statt Spinner: die Nachricht steht schon da, sie ist nur noch nicht
  // quittiert. Der Titel sagt, worauf sie wartet — ohne eine eigene Zeile zu
  // verbrauchen, die nach dem Bestätigen sofort wieder verschwinden müsste.
  const pendingStyle = pending ? { opacity: 0.6 } : undefined;
  const pendingTitle = pending ? t("composerQueuedNote") : undefined;

  if (message.sender_type === "system") {
    return (
      <div className="w-full px-4 md:px-5 py-2">
        <div
          data-testid="group-message-system"
          data-sender-type="system"
          title={pendingTitle}
          // Runden-Briefe werden vollständig gerendert, nicht geklemmt: sie sind
          // der Grund, warum jemand in die Runde zurückscrollt. Mono-Kleinschrift
          // hält sie leise, ohne sie zu verstecken.
          className="mx-auto max-w-[70ch] text-center font-mono text-[11px] leading-[1.7] whitespace-pre-wrap"
          style={{ color: C.textDim, ...pendingStyle }}
        >
          {message.body}
        </div>
      </div>
    );
  }

  if (message.sender_type === "user") {
    return (
      <div className="w-full px-4 md:px-5 py-2">
        <div
          data-testid="group-message-user"
          data-sender-type="user"
          title={pendingTitle}
          className={`${isOwn ? "ml-auto" : ""} w-fit max-w-[85%] min-w-0 rounded-xl px-3 py-2 text-[14px] leading-[1.6] whitespace-pre-wrap`}
          style={{
            background: C.bgElevated,
            border: `1px solid ${C.border}`,
            color: C.textPrimary,
            ...pendingStyle,
          }}
        >
          {/* Die Ausrichtung trägt den Sprecher visuell; ein Screenreader kann
              „rechts" nicht hören, also bleibt das Label als sr-only. */}
          {isOwn && <span className="sr-only">{t("you")}</span>}
          {message.body}
        </div>
      </div>
    );
  }

  const clock = formatClock(message.created_at);

  return (
    <div className="w-full px-4 md:px-5 py-2">
      {!groupWithPrevious && (
        <div className="mb-1 flex items-center gap-1.5">
          <EntityIcon value={senderEmoji} size={14} style={{ color: C.textSecondary }} />
          {senderName && (
            <span className="font-mono text-[11px]" style={{ color: C.textSecondary }}>
              {senderName}
            </span>
          )}
          {clock && (
            <span className="font-mono text-[11px]" style={{ color: C.textDim }}>
              {clock}
            </span>
          )}
        </div>
      )}
      <div
        data-testid="group-message-agent"
        data-sender-type="agent"
        title={pendingTitle}
        // Leseweite statt Panelbreite — dieselbe Kappung wie im 1:1-Chat, damit
        // ein Agentenbeitrag hier nicht anders liest als dort.
        className="max-w-[76ch] min-w-0 text-[14px] leading-[1.7] [&>*:last-child]:mb-0"
        style={pendingStyle}
      >
        <MarkdownContent content={message.body} />
      </div>
    </div>
  );
}
