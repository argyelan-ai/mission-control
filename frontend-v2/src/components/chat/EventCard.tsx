"use client";

/**
 * EventCard — eine Zeile „von aussen" als zugeklappte Karte.
 *
 * Auftrag, Hinweis, zugestellte Nachricht, Rueckmeldung eines Teamkollegen,
 * Meldung des Harness: bis 04.09.2026 sahen die alle gleich aus (Personen-
 * Icon, Dateiname, zehn Zeilen Rohtext) und waren im Verlauf nicht von
 * einander zu unterscheiden. Die Karte sagt zugeklappt auf einen Blick, WAS
 * es ist (Motiv + Wort) und WORUM es geht (Titel, sonst Absender); der Text
 * kommt erst auf Klick — wie bei der Werkzeug-Gruppe.
 *
 * Signal-Doktrin: Blau ist hier Herkunft („von Mission Control"), nicht
 * Betonung. Der Rahmen bleibt neutral; nur Motiv und Wort tragen die Farbe.
 */
import { useState } from "react";
import { useTranslations } from "next-intl";
import { ChevronRight } from "lucide-react";

import { C, STATUS_TEXT } from "@/lib/colors";
import type { MessageEvent, MessageSource } from "@/lib/chatTypes";
import { ClampedContent } from "./ClampedContent";
import { Motif, motifForSource } from "./Motif";

const MOTIF_SIZE = 22;

interface EventCardProps {
  ev: MessageEvent;
  /** Getrennt uebergeben, damit der Aufrufer die Herkunft nachweislich hat
   *  (`ev.source` ist optional; ohne sie bleibt es die schlichte Zeile). */
  source: MessageSource;
  live: boolean;
}

export function EventCard({ ev, source, live }: EventCardProps) {
  const t = useTranslations("chat.event");
  const [expanded, setExpanded] = useState(false);
  const { kind, title } = source;

  // Ein Teamkollege spricht fuer sich selbst; alles andere kommt ueber MC.
  const origin = kind === "teammate" ? ev.teammate ?? null : t("fromMc");
  // Zweite Spalte: der Auftragstitel, sonst der Absender (Dateiname). Beim
  // Teamkollegen steht der Name schon als Herkunft — nicht doppelt zeigen.
  const detail = title ?? (kind === "teammate" ? null : ev.teammate ?? null);

  return (
    <div className="w-full px-4 md:px-5 py-1" data-testid="event-card" data-kind={kind} data-live={live}>
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
        className="group flex w-full items-center gap-2.5 rounded-lg px-2.5 text-left min-h-[44px] md:min-h-[34px] cursor-pointer transition-colors"
        style={{
          background: expanded ? C.bgElevated : "transparent",
          border: `1px solid ${C.border}`,
        }}
      >
        <Motif kind={motifForSource(kind)} live={live} size={MOTIF_SIZE} className="shrink-0" />
        <span className="flex min-w-0 flex-1 items-baseline gap-1.5 text-[13px]">
          <span className="shrink-0 font-medium" style={{ color: STATUS_TEXT.info }}>
            {t(kind)}
          </span>
          {origin && (
            <span className="shrink-0" style={{ color: C.textMuted }}>
              · {origin}
            </span>
          )}
          {detail && (
            <span className="min-w-0 truncate" style={{ color: C.textSecondary }}>
              · {detail}
            </span>
          )}
          <span className="sr-only">{expanded ? t("collapse") : t("expand")}</span>
        </span>
        <ChevronRight
          size={13}
          className="shrink-0 transition-transform duration-150"
          style={{ color: C.textMuted, transform: expanded ? "rotate(90deg)" : undefined }}
          aria-hidden="true"
        />
      </button>

      {expanded && (
        <div className="mt-1 ml-3.5 pl-3 py-1" style={{ borderLeft: `1px solid ${C.border}` }}>
          <ClampedContent
            text={ev.text}
            testId="teammate-text"
            className="break-words whitespace-pre-wrap text-[13px] leading-[1.5]"
            style={{ color: C.textSecondary }}
          >
            {ev.text}
          </ClampedContent>
        </div>
      )}
    </div>
  );
}
