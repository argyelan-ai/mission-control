"use client";

/**
 * Ein delegierter Auftrag im Verlauf — mit seinem eigenen Protokoll darin.
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
import { Bot, ChevronRight, ChevronDown, CircleDot } from "lucide-react";
import { useTranslations } from "next-intl";
import { C, STATUS } from "@/lib/colors";
import { api } from "@/lib/api";
import type { ChatEvent, NotificationEvent, SubagentRun, ToolEvent } from "@/lib/chatTypes";
import { ChatMessage } from "./ChatMessage";
import { ToolRow } from "./ToolRow";
import { ThinkingRow } from "./ThinkingRow";
import { CommandRow } from "./CommandRow";

const DESC_MAX = 90;

/** Farbnamen aus dem Steckbrief auf die Palette abbilden. Ein unbekannter
 *  Name faellt auf die Akzentfarbe zurueck — nie auf eine rohe CSS-Farbe aus
 *  fremden Daten. */
const RUN_COLORS: Record<string, string> = {
  green: STATUS.online,
  red: STATUS.error,
  yellow: STATUS.warning,
  blue: C.accent,
};

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

export function AgentCard({
  ev,
  run,
  notice,
  agentId,
}: {
  ev: ToolEvent;
  run?: SubagentRun;
  /** Die Hintergrund-Meldung der CLI zu genau diesem Aufruf, falls sie
   *  eingetroffen ist. */
  notice?: NotificationEvent;
  agentId: string;
}) {
  const t = useTranslations("sessions");
  const [expanded, setExpanded] = useState(false);

  const detail = (ev.detail ?? {}) as Record<string, unknown>;
  /* Aus dem Steckbrief, sonst aus dem Aufruf. `ev.title` bleibt aussen vor —
     der traegt bereits "Agent: " im Text, das gaebe eine Doppelung. */
  const name =
    run?.name ?? str(detail.name) ?? run?.agentType ?? str(detail.subagent_type)
    ?? t("agentCardFallbackName");
  const description = run?.description ?? str(detail.description);
  const model = run?.model ?? str(detail.model);
  const dot = run?.color ? RUN_COLORS[run.color] ?? C.accent : null;

  /* Zuerst die BEOBACHTUNG: die CLI meldet selbst, wenn der Auftrag endet.
     Ohne sie blieb nur die Vermutung "kein Ergebnis heisst laeuft noch" — die
     nach einem Abbruch dauerhaft falsch stehen bleibt.
     Nie aus dem Ergebnis-TEXT: dort stehen Spawn-Metadaten, die sich selbst
     als nicht zitierfaehig bezeichnen. Sie werden gedeutet, nicht abgedruckt. */
  const status =
    notice?.status === "failed" || ev.status === "error"
      ? t("agentCardFailed")
      : notice?.status === "completed" || ev.result !== null
        ? t("agentCardDone")
        : t("agentCardRunning");

  const history = useQuery({
    queryKey: ["subagent-history", agentId, run?.runId],
    queryFn: () => api.chat.subagentHistory(agentId, run!.runId),
    enabled: expanded && !!run,
  });

  const Chevron = expanded ? ChevronDown : ChevronRight;
  const events = history.data?.events ?? [];

  return (
    <div className="w-full px-4 md:px-5 py-1.5">
      <div
        className="w-full rounded-lg overflow-hidden"
        style={{ border: `1px solid ${C.border}`, background: C.bgElevated }}
      >
        {run ? (
          <button
            type="button"
            data-testid="agent-card-toggle"
            onClick={() => setExpanded((v) => !v)}
            aria-expanded={expanded}
            aria-label={expanded ? t("agentCardCollapse") : t("agentCardExpand")}
            className="w-full flex items-center gap-2 px-3 min-h-[44px] md:min-h-[34px] text-left bg-transparent border-0 cursor-pointer"
          >
            <Chevron size={13} className="shrink-0" style={{ color: C.textMuted }} />
            <Head name={name} description={description} model={model} dot={dot} status={status} />
          </button>
        ) : (
          /* Kein sicher zugeordneter Lauf -> kein Aufklappen. Lieber karg als
             das Protokoll eines fremden Auftrags anbieten. */
          <div
            data-testid="agent-card-static"
            className="w-full flex items-center gap-2 px-3 min-h-[44px] md:min-h-[34px]"
          >
            <Bot size={13} className="shrink-0" style={{ color: C.textMuted }} />
            <Head name={name} description={description} model={model} dot={dot} status={status} />
          </div>
        )}

        {expanded && run && (
          <div style={{ borderTop: `1px solid ${C.border}` }}>
            {history.isLoading && (
              <p className="px-3 py-2 text-[12px]" style={{ color: C.textMuted }}>
                {t("agentCardLoading")}
              </p>
            )}
            {history.isError && (
              <p className="px-3 py-2 text-[12px]" style={{ color: C.textMuted }}>
                {t("agentCardNoTranscript")}
              </p>
            )}
            {events.map(renderChild)}
          </div>
        )}
      </div>
    </div>
  );
}

function Head({
  name,
  description,
  model,
  dot,
  status,
}: {
  name: string;
  description: string | null;
  model: string | null;
  dot: string | null;
  status: string;
}) {
  return (
    <>
      {dot ? (
        <CircleDot size={11} className="shrink-0" style={{ color: dot }} />
      ) : (
        <Bot size={13} className="shrink-0" style={{ color: C.accent }} />
      )}
      <span className="text-[13px] shrink-0 font-medium" style={{ color: C.textSecondary }}>
        {name}
      </span>
      {description && (
        <span className="text-[12px] truncate hidden sm:inline" style={{ color: C.textMuted }}>
          {short(description)}
        </span>
      )}
      <span className="ml-auto flex items-center gap-2 shrink-0">
        {model && (
          <span className="text-[10px] font-mono hidden md:inline" style={{ color: C.textMuted }}>
            {model}
          </span>
        )}
        <span
          className="text-[10px] font-mono px-1.5 py-0.5 rounded"
          style={{ background: C.bgHover, color: C.textMuted }}
        >
          {status}
        </span>
      </span>
    </>
  );
}
