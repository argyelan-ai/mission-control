"use client";

/**
 * GroupGateCard — die Gruppe hält an und fragt Mark. Sitzt direkt über dem
 * Composer, damit Frage und Antwortfeld ein Block sind.
 *
 * Design-Entscheidung (nicht offensichtlich): die Karte ist kein Modal und
 * blockiert nichts. Der Composer darunter bleibt bedienbar — die Fusszeile
 * sagt das ausdrücklich —, weil eine echte Antwort öfter ein Satz ist als ein
 * Ja/Nein. Die zwei Knöpfe sind die Abkürzung, nicht der einzige Weg.
 */
import { C } from "@/lib/colors";
import { useTranslations } from "next-intl";

interface GroupGateCardProps {
  question: string;
  onApprove: () => void;
  onReject: () => void;
  /** Antwort ist unterwegs — beide Knöpfe aus, damit ein Doppelklick nicht
   *  zwei Runden-Entscheide absetzt. */
  busy?: boolean;
}

export function GroupGateCard({ question, onApprove, onReject, busy = false }: GroupGateCardProps) {
  const t = useTranslations("sessions.groups");

  return (
    <div
      className="flex flex-col gap-2 rounded-lg border p-3"
      style={{ borderColor: C.borderAccent, backgroundColor: C.accentSubtle }}
    >
      <span
        className="font-mono text-[10px] uppercase tracking-wider"
        style={{ color: C.accent }}
      >
        {t("gateTitle")}
      </span>

      <p
        className="whitespace-pre-wrap text-sm leading-snug"
        style={{ color: C.textPrimary }}
      >
        {question}
      </p>

      <div className="flex flex-wrap gap-2 pt-0.5">
        <button
          type="button"
          disabled={busy}
          onClick={onApprove}
          className="rounded-md px-3 py-1.5 text-sm font-medium transition-opacity disabled:cursor-not-allowed disabled:opacity-40"
          style={{ backgroundColor: C.accent, color: C.onAccent }}
        >
          {t("gateApprove")}
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={onReject}
          className="rounded-md border px-3 py-1.5 text-sm transition-colors disabled:cursor-not-allowed disabled:opacity-40"
          style={{ borderColor: C.border, color: C.textSecondary }}
        >
          {t("gateReject")}
        </button>
      </div>

      <span className="text-[11px] leading-snug" style={{ color: C.textMuted }}>
        {t("gateFreeText")}
      </span>
    </div>
  );
}
