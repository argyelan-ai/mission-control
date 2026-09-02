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
 *
 * Agenten-Beiträge starten ZUGEKLAPPT (Marks Befund 02.09.2026: offen stehende
 * Beiträge machen den Raum laut). Sichtbar bleibt eine Kopfzeile — Avatar,
 * Name, Uhrzeit, erste Zeile — die als Griff dient: wer lesen will, macht
 * gezielt den richtigen Beitrag auf. Nur das Lead-Urteil (`defaultOpen`)
 * steht von sich aus offen, es ist das Ergebnis der Runde. Die frühere
 * 3-Zeilen-Klemme ist damit entfallen; ein offener Beitrag ist ganz offen.
 * System-Briefe behalten ihren eigenen Aufklapper (Maschinen-Auftrag, man
 * liest ihn nur auf Verdacht). Siehe ADR-075, Punkt F.
 */
import { useState } from "react";
import { useTranslations } from "next-intl";
import { ChevronDown, ChevronRight } from "lucide-react";
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
  /** Weitere Maschinen-Aufträge, die direkt auf diesen folgten. Sie werden
   *  unter DIESEM Aufklapper mitgeführt statt eigene Zeilen zu bekommen:
   *  pro Runde standen sonst Brief, Timeout-Notiz und Synthese-Auftrag als
   *  drei Zeilen zwischen zwei Beiträgen, die man vergleichen will. */
  alsoContains?: string[];
  /** Offen starten — nur für das Lead-Urteil am Rundenende. Alles andere
   *  klappt zu, damit der Raum ruhig bleibt. */
  defaultOpen?: boolean;
}

/** Ab dieser Länge wird eine System-Nachricht zugeklappt. Runden-Briefe und
 *  Synthese-Aufträge liegen bei 1000–5000 Zeichen, Timeout-Notizen bei ~60 —
 *  dazwischen ist viel Luft, die Grenze muss nicht fein justiert sein. */
const SYSTEM_COLLAPSE_CHARS = 240;

/** Die eine Zeile auf dem zugeklappten Beitrag: erste nicht-leere Zeile,
 *  von Markdown-Zierrat befreit. Nichts wird geparst, was man nicht sieht —
 *  nur Überschriftszeichen, Betonung, Listenpunkte, Zitatzeichen und die
 *  Pipes einer Tabellenzeile (die wird zur Phrase „A · B"). Gekürzt wird per
 *  CSS (`truncate`), nicht hier — die Breite kennt nur der Bildschirm. */
export function contributionExcerpt(body: string): string {
  const line = (body ?? "").split("\n").find((l) => l.trim()) ?? "";
  let out = line.trim();
  if (out.startsWith("|")) {
    out = out
      .split("|")
      .map((c) => c.trim())
      .filter(Boolean)
      .join(" · ");
  }
  return out
    .replace(/^#{1,6}\s+/, "")
    .replace(/^(?:[-*+]|\d+[.)])\s+/, "")
    .replace(/^>\s*/, "")
    .replace(/(\*\*|__|~~|`)/g, "")
    .replace(/(^|[^\w])[*_](?=\S)/g, "$1")
    .replace(/(?<=\S)[*_](?=[^\w]|$)/g, "")
    .replace(/\s+/g, " ")
    .trim();
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
  alsoContains,
  defaultOpen = false,
}: GroupMessageProps) {
  const t = useTranslations("sessions.groups");
  // Zugeklappt starten: der Verlauf gehört den Beiträgen, nicht den Aufträgen
  // der Engine. Wer wissen will, was genau beauftragt wurde, klappt auf.
  const [systemOpen, setSystemOpen] = useState(false);
  // Beiträge starten zu (Marks Befund 02.09.2026) — ausser dem Lead-Urteil.
  // Der Zustand lebt pro Nachricht (Key = message.id im Verlauf) und bleibt
  // deshalb beim Nachladen/Scrollen erhalten.
  const [open, setOpen] = useState(defaultOpen);

  const pending = message.pending === true;
  // Gedimmt statt Spinner: die Nachricht steht schon da, sie ist nur noch nicht
  // quittiert. Der Titel sagt, worauf sie wartet — ohne eine eigene Zeile zu
  // verbrauchen, die nach dem Bestätigen sofort wieder verschwinden müsste.
  const pendingStyle = pending ? { opacity: 0.6 } : undefined;
  const pendingTitle = pending ? t("composerQueuedNote") : undefined;

  if (message.sender_type === "system") {
    const body = message.body ?? "";
    const lines = body.split("\n");
    // Die erste nicht-leere Zeile IST bereits die Zusammenfassung — der
    // Runden-Brief beginnt mit „# Gruppe: … — Runde 1/2", der Synthese-Auftrag
    // mit „@beta — Synthese-Turn Runde 2/2". Deshalb wird hier nichts
    // geparst und nichts geraten: was oben steht, steht auf dem Knopf.
    const summary = (lines.find((l) => l.trim()) ?? "").replace(/^#+\s*/, "").trim();
    const merged = alsoContains ?? [];
    // Die Trennlinie macht sichtbar, dass hier mehrere Auftraege stehen —
    // sonst laese sich der aufgeklappte Block als ein einziger langer Text.
    const fullBody = merged.length
      ? [body, ...merged].join("\n\n────────\n\n")
      : body;
    const long =
      merged.length > 0 || body.length > SYSTEM_COLLAPSE_CHARS || lines.length > 3;

    // Kurze Systemzeilen (Timeout-Notiz, Statuswechsel) bleiben offen: sie sind
    // eine Zeile lang und genau dann wichtig, wenn man sie nicht sucht.
    if (!long) {
      return (
        <div className="w-full px-4 md:px-5 py-2">
          <div
            data-testid="group-message-system"
            data-sender-type="system"
            title={pendingTitle}
            className="mx-auto max-w-[70ch] text-center font-mono text-[11px] leading-[1.7] whitespace-pre-wrap"
            style={{ color: C.textDim, ...pendingStyle }}
          >
            {body}
          </div>
        </div>
      );
    }

    // Lange Maschinen-Aufträge (Runden-Brief, Synthese-Turn) walzen den Verlauf
    // sonst zu: mehrere tausend Zeichen Anweisung zwischen zwei Beiträgen, die
    // der Leser eigentlich vergleichen will. Zugeklappt wie ein Denk-Block —
    // sichtbar bleibt, DASS die Engine das Wort erteilt hat, und an wen.
    return (
      <div className="w-full px-4 md:px-5 py-2">
        <div
          data-testid="group-message-system"
          data-sender-type="system"
          className="mx-auto max-w-[70ch]"
          style={pendingStyle}
        >
          <button
            type="button"
            onClick={() => setSystemOpen((v) => !v)}
            aria-expanded={systemOpen}
            title={pendingTitle}
            data-testid="group-system-toggle"
            className="w-full flex items-center gap-1.5 text-left bg-transparent border-0 p-0 cursor-pointer"
          >
            {systemOpen ? (
              <ChevronDown size={12} className="shrink-0" style={{ color: C.textDim }} />
            ) : (
              <ChevronRight size={12} className="shrink-0" style={{ color: C.textDim }} />
            )}
            <span
              // textMuted, nicht textDim: colors.ts sagt zu textDim
              // ausdruecklich „decoration / inactive icons ONLY — never body
              // text". Die Beschriftung IST Text; der Chevron daneben darf
              // dekorativ bleiben.
              className="font-mono text-[11px] truncate"
              style={{ color: C.textMuted }}
            >
              {merged.length ? `${summary} · +${merged.length}` : summary}
            </span>
          </button>

          {systemOpen && (
            <pre
              data-testid="group-system-body"
              className="mt-1.5 ml-5 max-h-[360px] overflow-auto whitespace-pre-wrap font-mono text-[11px] leading-[1.7] p-2 rounded-sm scroll-quiet"
              style={{
                background: "var(--color-bg-elevated)",
                color: C.textMuted,
                border: `1px solid ${C.border}`,
              }}
            >
              {fullBody}
            </pre>
          )}
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
  const excerpt = contributionExcerpt(message.body);

  return (
    // Sprecher-Rinne statt bündiger Kante (Operator-Befund 22.08.2026): eine
    // schmale Spalte links hält den Avatar, der Text rückt auf Namenshöhe ein
    // — so liest sich der Verlauf als Raum mit Leuten darin, nicht als
    // Protokoll. Die Kopfzeile bleibt auch beim selben Sprecher stehen: sie
    // IST zugeklappt der Beitrag und der Griff zum Öffnen.
    <div className={`w-full px-4 md:px-5 ${groupWithPrevious ? "pt-0.5 pb-1" : "pt-2 pb-1"}`}>
      <div className="flex gap-2.5">
        <div
          data-testid="group-speaker-gutter"
          className="w-7 shrink-0 flex justify-center pt-[3px]"
        >
          <EntityIcon value={senderEmoji} size={18} style={{ color: C.textSecondary }} />
        </div>
        <div
          data-testid="group-message-agent"
          data-sender-type="agent"
          className="min-w-0 flex-1"
          style={pendingStyle}
        >
          <button
            type="button"
            onClick={() => setOpen((v) => !v)}
            aria-expanded={open}
            title={pendingTitle}
            data-testid="group-contribution-toggle"
            // Ganze Zeile klickbar, ohne Rahmen: der Griff soll nicht lauter
            // sein als der Text, den er zeigt. Aufhellen statt Farbwechsel.
            className="w-full flex items-baseline gap-2 text-left bg-transparent border-0 p-0 cursor-pointer opacity-90 hover:opacity-100 transition-opacity"
          >
            {open ? (
              <ChevronDown size={12} className="shrink-0 self-center" style={{ color: C.textDim }} />
            ) : (
              <ChevronRight size={12} className="shrink-0 self-center" style={{ color: C.textDim }} />
            )}
            {senderName && (
              <span
                className="shrink-0 font-mono text-[11px] font-medium"
                style={{ color: C.textSecondary }}
              >
                {senderName}
              </span>
            )}
            {clock && (
              <span className="shrink-0 font-mono text-[11px]" style={{ color: C.textDim }}>
                {clock}
              </span>
            )}
            {!open && (
              // Auszug nur zugeklappt: offen steht die erste Zeile ohnehin
              // direkt darunter, doppelt wäre sie Rauschen.
              <span
                className="min-w-0 truncate text-[13px]"
                style={{ color: C.textMuted }}
              >
                {excerpt}
              </span>
            )}
          </button>
          {open && (
            <div
              data-testid="group-contribution-body"
              // Leseweite statt Panelbreite — dieselbe Kappung wie im 1:1-Chat,
              // damit ein Agentenbeitrag hier nicht anders liest als dort.
              className="mt-1 max-w-[76ch] min-w-0 text-[14px] leading-[1.7] [&>*:last-child]:mb-0"
            >
              <MarkdownContent content={message.body} />
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
