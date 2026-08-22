"use client";

/**
 * GroupStatusLine — EINE wahrhaftige Zeile über dem Gruppen-Composer: Punkt +
 * Text (+ optional die laufenden Kosten rechts). Gegenstück zu
 * `chat/StatusLine.tsx`, nur speist sie sich aus dem Gruppen-SSE-Kanal statt
 * aus dem Transkript-Tailer eines einzelnen Agenten.
 *
 * Design-Entscheidung (nicht offensichtlich): `connected` schlägt JEDEN
 * fachlichen Status. Der Status kommt über denselben Stream, der gerade
 * abgerissen ist — er ist also veraltet, sobald die Verbindung weg ist.
 * Lieber „Status unbekannt" sagen als eine hübsche Lüge rendern.
 */
import { C, STATUS_TEXT } from "@/lib/colors";
import { useTranslations } from "next-intl";
import type { GroupStatus, GroupStreamState } from "@/lib/groupTypes";

interface GroupStatusLineProps {
  status: GroupStatus;
  state: GroupStreamState;
  connected: boolean;
  /** Kostendeckel der Gruppe (USD) — null/undefined = kein Budget gesetzt. */
  budgetUsd?: number | null;
  /** Bisher verbrauchte Kosten (USD) — null/undefined = noch nichts gemessen. */
  spentUsd?: number | null;
}

/** Ab hier färbt sich die Kostenanzeige warnend: der Budget-Stopp greift erst
 *  an der Rundengrenze, kann also leicht überschiessen — die Warnung muss
 *  daher VOR dem Limit stehen, nicht daran. */
const BUDGET_WARN_RATIO = 0.85;

export function GroupStatusLine({
  status,
  state,
  connected,
  budgetUsd,
  spentUsd,
}: GroupStatusLineProps) {
  const t = useTranslations("sessions.groups");

  let dotColor: string = C.textMuted;
  let textColor: string = C.textMuted;
  let label: string;
  let pulse = false;

  if (!connected) {
    dotColor = C.warning;
    textColor = STATUS_TEXT.warning;
    label = t("statusDisconnected");
  } else if (status === "waiting_gate") {
    dotColor = C.warning;
    textColor = STATUS_TEXT.warning;
    label = t("statusWaitingForYou");
    pulse = true;
  } else if (status === "paused") {
    dotColor = C.textMuted;
    textColor = C.textMuted;
    label = t("statusPaused");
  } else if (status === "done") {
    dotColor = C.textDim;
    textColor = C.textDim;
    label = t("statusDone");
  } else if (status === "failed") {
    // Ohne diesen Zweig fiele eine gescheiterte Gruppe in den Else-Fall und
    // meldete „Bereit" — genau die Sorte plausibler Unwahrheit, die diese
    // Zeile verhindern soll.
    dotColor = C.error;
    textColor = STATUS_TEXT.error;
    label = t("statusFailed");
  } else if (status === "running" && state.activeSpeaker) {
    dotColor = C.info;
    textColor = STATUS_TEXT.info;
    label = t("statusSynthesis", { name: state.activeSpeaker });
    pulse = true;
  } else if (status === "running" && state.pendingSpeakers.length > 0) {
    dotColor = C.info;
    textColor = STATUS_TEXT.info;
    label = t("statusWaitingFor", {
      round: state.roundNo ?? 0,
      max: state.maxRounds ?? 0,
      names: state.pendingSpeakers.join(", "),
    });
    pulse = true;
  } else if (status === "running") {
    dotColor = C.info;
    textColor = STATUS_TEXT.info;
    label = t("statusBetweenRounds", { round: state.roundNo ?? 0 });
  } else {
    label = t("statusIdle");
  }

  // Kosten nur zeigen, wenn wir sie WISSEN. Ohne Messwert bleibt die Zeile leer
  // statt „0.00" zu behaupten — das wäre eine Aussage, kein fehlender Wert.
  const hasCost = spentUsd !== null && spentUsd !== undefined;
  const hasBudget = budgetUsd !== null && budgetUsd !== undefined;
  const costValue = hasCost
    ? hasBudget
      ? `${spentUsd.toFixed(2)} / ${budgetUsd.toFixed(2)}`
      : spentUsd.toFixed(2)
    : null;
  const budgetTight =
    hasCost && hasBudget && budgetUsd > 0 && spentUsd / budgetUsd >= BUDGET_WARN_RATIO;

  return (
    // Linke Kante auf der Nachrichtenspalte (px-4 md:px-5), damit die Zeile als
    // letzte Zeile des Gesprächs liest und nicht als Composer-Chrom.
    <div
      className="flex items-center gap-2 px-4 md:px-5 pb-1.5 text-[12px]"
      aria-live="polite"
    >
      <span className="relative inline-flex h-1.5 w-1.5 shrink-0">
        <span className="absolute inset-0 rounded-full" style={{ backgroundColor: dotColor }} />
        {pulse && (
          <span
            className="absolute inset-0 animate-ping rounded-full"
            style={{ backgroundColor: dotColor, opacity: 0.6 }}
          />
        )}
      </span>
      <span className="min-w-0 truncate" style={{ color: textColor }}>
        {label}
      </span>
      {costValue !== null && (
        <span
          className="ml-auto shrink-0 font-mono tabular-nums text-[11px]"
          style={{ color: budgetTight ? STATUS_TEXT.warning : C.textDim }}
        >
          {t("statusCost", { cost: costValue })}
        </span>
      )}
    </div>
  );
}
