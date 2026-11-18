"use client";

/**
 * Ein delegierter Auftrag im Verlauf — mit seinem eigenen Protokoll darin.
 *
 * Seit der Helfer-Zeile (N1) eine ruhige ZEILE in deren aufgeklappter Liste,
 * keine eigene Karte mehr: Zustandspunkt, Name, Auftrag, Dauer. Der Zustand
 * kommt aus `helpers.ts` (beobachtet, nie aus "hat ein Ergebnis").
 *
 * Vorher war ein Subagenten-Auftrag im Chat nur eine Werkzeug-Zeile ohne
 * Inhalt, weil sein Gespraech gar nicht im Hauptstrom steht: Claude Code
 * schreibt es in eine eigene Datei (live gemessen 22.08.2026 — 0 Zeilen mit
 * `isSidechain: true` im Hauptstrom, dafuer 679 Subagenten-Dateien auf der
 * Platte). Diese Karte holt es von dort, aber erst beim Aufklappen.
 *
 * Die Karte funktioniert VOLLSTAENDIG ohne Steckbrief: in knapp der Haelfte
 * der Faelle laesst sich der Lauf keinem Aufruf sicher zuordnen (siehe
 * `agentRuns.ts`). Dann zeigt sie, was im Aufruf selbst steht, und laesst den
 * Aufklapp-Knopf weg — statt einen fremden Verlauf anzubieten.
 */

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ChevronRight } from "lucide-react";
import { useLocale, useTranslations } from "next-intl";
import { C, STATUS_TEXT } from "@/lib/colors";
import { formatDuration } from "@/lib/taskDetail/format";
import { helperElapsedSeconds, type HelperState, type HelperView } from "./helpers";
import { api } from "@/lib/api";
import type { ChatEvent, SubagentRun, ToolEvent } from "@/lib/chatTypes";
import { ChatMessage } from "./ChatMessage";
import { ToolRow } from "./ToolRow";
import { ThinkingRow } from "./ThinkingRow";
import { CommandRow } from "./CommandRow";

const DESC_MAX = 90;

function str(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function short(text: string): string {
  const line = text.split("\n")[0]?.trim() ?? "";
  return line.length > DESC_MAX ? `${line.slice(0, DESC_MAX)}…` : line;
}

/** Ein Kind-Ereignis mit denselben Zeilen wie der Hauptverlauf. */
function renderChild(ev: ChatEvent) {
  switch (ev.kind) {
    case "message":
      return <ChatMessage key={ev.uuid} ev={ev} />;
    case "tool":
      return <ToolRow key={ev.toolUseId ?? ev.uuid} ev={ev} detailLevel="normal" />;
    case "thinking":
      return <ThinkingRow key={ev.uuid} ev={ev} detailLevel="normal" />;
    case "command":
      return <CommandRow key={ev.uuid} ev={ev} detailLevel="normal" />;
    default:
      return null;
  }
}

/** Der Zustandspunkt: "arbeitet" ist Info (aktive Arbeit), "fehlgeschlagen"
 *  eine Warnung (ein Helfer, nicht der Zug), "fertig" ist keine Nachricht
 *  und bleibt neutral — so steht pro Liste hoechstens eine Statusfarbe da,
 *  ausser es arbeitet einer UND einer scheiterte. */
export const HELPER_DOT: Record<HelperState, string> = {
  working: STATUS_TEXT.info,
  done: C.textMuted,
  failed: STATUS_TEXT.warning,
};

const STATE_KEY: Record<HelperState, string> = {
  working: "helperLine.stateWorking",
  done: "helperLine.stateDone",
  failed: "helperLine.stateFailed",
};

export function AgentCard({
  ev,
  run,
  agentId,
  helper,
  now,
}: {
  ev: ToolEvent;
  run?: SubagentRun;
  agentId: string;
  /** Zustand und Dauer dieses Helfers (aus `collectHelpers`). */
  helper: HelperView;
  /** Uhr der Zeile darueber — damit alle Zeilen dieselbe Zeit zeigen. */
  now?: Date;
}) {
  const t = useTranslations("sessions");
  const locale = useLocale();
  const [expanded, setExpanded] = useState(false);

  const detail = (ev.detail ?? {}) as Record<string, unknown>;
  /* Aus dem Steckbrief, sonst aus dem Aufruf. `ev.title` bleibt aussen vor —
     der traegt bereits "Agent: " im Text, das gaebe eine Doppelung. */
  // omp: ein Aufruf, mehrere Helfer — sein Auftragstext gehoert keinem davon
  // allein, darum dort die Rolle statt eines geliehenen Texts.
  const job = ev.name === "task" ? helper.agent : run?.description ?? str(detail.description);
  /* Vorne steht, was den Helfer unterscheidet: sein eigener Name, sonst sein
     Auftrag. Der Typ ("general-purpose") steht bei fast allen gleich da und
     sagt nichts — er kommt nur, wenn sonst nichts da ist. */
  const ownName = helper.name ?? run?.name ?? str(detail.name);
  const name =
    ownName ?? job ?? run?.agentType ?? str(detail.subagent_type) ?? helper.agent
    ?? t("agentCardFallbackName");
  const description = ownName ? job : null;
  const seconds = helperElapsedSeconds(helper, now);
  // K17: Dauern erst ab 1 min, immer in der Dauer-Form.
  const duration = seconds != null && seconds >= 60 ? formatDuration(seconds, locale) : null;

  const history = useQuery({
    queryKey: ["subagent-history", agentId, run?.runId],
    queryFn: () => api.chat.subagentHistory(agentId, run!.runId),
    enabled: expanded && !!run,
  });
  const events = history.data?.events ?? [];

  const head = (
    <>
      <span
        aria-hidden="true"
        className="shrink-0 w-1.5 h-1.5 rounded-full"
        style={{ background: HELPER_DOT[helper.state] }}
      />
      <span
        className={description ? "text-sm shrink-0 truncate max-w-1/2" : "text-sm truncate min-w-0"}
        style={{ color: C.textSecondary }}
      >
        {description ? name : short(name)}
      </span>
      {description && (
        <span className="text-xs truncate min-w-0" style={{ color: C.textMuted }}>
          {short(description)}
        </span>
      )}
      <span className="ml-auto flex items-center gap-2 shrink-0 text-xs tabular-nums" style={{ color: C.textMuted }}>
        {duration && <span data-testid="helper-duration">{duration}</span>}
        <span data-testid="helper-state">{t(STATE_KEY[helper.state])}</span>
      </span>
    </>
  );

  return (
    <div className="w-full" data-testid="helper-row">
      {run ? (
        <button
          type="button"
          data-testid="agent-card-toggle"
          onClick={() => setExpanded((v) => !v)}
          aria-expanded={expanded}
          aria-label={expanded ? t("agentCardCollapse") : t("agentCardExpand")}
          className="w-full flex items-center gap-2 px-2 min-h-[44px] md:min-h-[34px] text-left bg-transparent border-0 cursor-pointer"
        >
          {head}
          <ChevronRight
            size={13}
            className="shrink-0 transition-transform duration-150"
            style={{ color: C.textMuted, transform: expanded ? "rotate(90deg)" : undefined }}
            aria-hidden="true"
          />
        </button>
      ) : (
        /* Kein sicher zugeordneter Lauf -> kein Aufklappen. Lieber karg als
           das Protokoll eines fremden Auftrags anbieten. */
        <div
          data-testid="agent-card-static"
          className="w-full flex items-center gap-2 px-2 min-h-[44px] md:min-h-[34px]"
        >
          {head}
        </div>
      )}

      {expanded && run && (
        <div className="ml-3.5 pl-1" style={{ borderLeft: `1px solid ${C.border}` }}>
          {history.isLoading && (
            <p className="px-3 py-2 text-xs" style={{ color: C.textMuted }}>
              {t("agentCardLoading")}
            </p>
          )}
          {history.isError && (
            <p className="px-3 py-2 text-xs" style={{ color: C.textMuted }}>
              {t("agentCardNoTranscript")}
            </p>
          )}
          {events.map(renderChild)}
        </div>
      )}
    </div>
  );
}
