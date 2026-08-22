"use client";

/**
 * ResultDocPanel — das lebende Ergebnis-Dokument einer Gruppe (ADR-075).
 *
 * Kein Datei-Betrachter mehr, sondern ein Bericht (Umbau 22.08.2026).
 * Vorher lief das Dokument als roher Markdown-Fluss durch: zuoberst das
 * Vorspann-Zitat für den Lead-Agenten, darunter das Ziel, das der Operator
 * selbst getippt hatte — und die Antwort erst an dritter Stelle, auf dem Handy
 * zwei Bildschirme tief. Operator-Befund: „das soll übersichtlicher sein und
 * wirklich an einen Gruppenchat erinnern."
 *
 * Jetzt: Verdikt in der Kopfzone (ohne Scrollen), Abschnitte als aufklappbare
 * Einträge, Ziel ans Ende. Die Sortierung steckt in `parseDocOutline` und ist
 * dort ohne React geprüft.
 *
 * Nicht offensichtlich: „neueste Version" ist bewusst KEIN Snapshot, sondern
 * die Datei selbst (`document(groupId)` ohne `version`). Ein Snapshot friert
 * den Stand am Rundenende ein, die Datei kann seither weitergeschrieben worden
 * sein. Darum ist `viewVersion === null` ein eigener Zustand und nicht einfach
 * der letzte Eintrag der Versionsliste.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslations } from "next-intl";
import { Check, ChevronDown, ChevronRight, Copy, Info } from "lucide-react";
import { api } from "@/lib/api";
import { C } from "@/lib/colors";
import { MarkdownContent } from "@/components/chat/MarkdownContent";
import { parseDocOutline } from "@/lib/docOutline";

interface ResultDocPanelProps {
  groupId: string;
  /** Version des zuletzt geschriebenen Dokuments (aus dem SSE-Zustand) — dient
   *  hier als Änderungs-Signal, nicht als Anzeigewert. */
  latestVersion: number | null;
  /** Die Gruppe schreibt gerade am Dokument. */
  updating?: boolean;
}

export function ResultDocPanel({ groupId, latestVersion, updating = false }: ResultDocPanelProps) {
  const t = useTranslations("sessions.groups");

  const [snapshotRounds, setSnapshotRounds] = useState<number[]>([]);
  /** null = aktueller Datei-Stand, sonst die Runde des angezeigten Snapshots. */
  const [viewVersion, setViewVersion] = useState<number | null>(null);
  /** null = noch nie geladen; "" = geladen und leer (auch der Fehlerfall). */
  const [content, setContent] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [openIds, setOpenIds] = useState<Set<string>>(new Set());
  const [noteOpen, setNoteOpen] = useState(false);

  // Gruppenwechsel: bewusst beim Rendern zurückgesetzt und nicht im Effekt.
  // Ein Effekt läuft erst NACH dem Rendern — für einen Frame stünde also das
  // Dokument der alten Gruppe unter dem Kopf der neuen.
  const [renderedGroupId, setRenderedGroupId] = useState(groupId);
  if (renderedGroupId !== groupId) {
    setRenderedGroupId(groupId);
    setViewVersion(null);
    setContent(null);
    setOpenIds(new Set());
    setNoteOpen(false);
  }

  useEffect(() => {
    let cancelled = false;
    api.groups
      .rounds(groupId)
      .then((res) => {
        if (cancelled) return;
        setSnapshotRounds(
          (res.rounds ?? [])
            .filter((r) => r.has_doc_snapshot)
            .map((r) => r.round_no)
            .sort((a, b) => a - b),
        );
      })
      .catch(() => {
        if (!cancelled) setSnapshotRounds([]);
      });
    return () => {
      cancelled = true;
    };
  }, [groupId, latestVersion]);

  // Der Stream meldet eine neue Version, bevor `/rounds` den Snapshot führt.
  // Ohne dieses Einmischen würde die Zeitleiste in genau diesem Moment einen
  // Punkt verlieren und wieder auftauchen lassen — ein Zappeln, das der
  // Nutzer als Fehler liest.
  const versions = useMemo(() => {
    if (latestVersion == null || snapshotRounds.includes(latestVersion)) return snapshotRounds;
    return [...snapshotRounds, latestVersion].sort((a, b) => a - b);
  }, [snapshotRounds, latestVersion]);

  // Ein eingefrorener Snapshot ändert sich nie — nur der Live-Stand wird bei
  // einer neuen Version nachgeladen.
  const liveKey = viewVersion == null ? latestVersion : null;

  useEffect(() => {
    let cancelled = false;
    const load =
      viewVersion == null ? api.groups.document(groupId) : api.groups.document(groupId, viewVersion);
    load
      .then((doc) => {
        if (!cancelled) setContent(doc?.content ?? "");
      })
      .catch(() => {
        // Still: ein fehlendes Dokument ist der Normalfall vor der ersten
        // Runde, kein Vorfall, über den man den Nutzer belehren müsste.
        if (!cancelled) setContent("");
      });
    return () => {
      cancelled = true;
    };
  }, [groupId, viewVersion, liveKey]);

  useEffect(() => {
    if (!copied) return;
    const timer = setTimeout(() => setCopied(false), 2000);
    return () => clearTimeout(timer);
  }, [copied]);

  const outline = useMemo(() => parseDocOutline(content ?? ""), [content]);
  const hasContent = content != null && !outline.empty;

  const handleCopy = useCallback(async () => {
    if (!content) return;
    try {
      await navigator.clipboard.writeText(content);
      setCopied(true);
    } catch {
      // Ohne HTTPS (und in jsdom) gibt es `navigator.clipboard` gar nicht.
      // Dann passiert nichts — eine Erfolgsmeldung wäre gelogen.
    }
  }, [content]);

  const toggleSection = useCallback((id: string) => {
    setOpenIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  return (
    <div className="flex flex-col h-full min-h-0">
      {/* ── Kopfzeile: was ist das, und welche Fassung sehe ich ── */}
      <div
        className="flex items-center gap-2 px-3 py-2 border-b shrink-0"
        style={{ borderColor: C.border }}
      >
        <span
          className="text-[11px] font-mono uppercase tracking-wider"
          style={{ color: C.textMuted }}
        >
          {t("resultPanel")}
        </span>
        {updating && (
          <span className="text-[11px] animate-pulse" style={{ color: C.textMuted }} aria-live="polite">
            {t("resultUpdating")}
          </span>
        )}
      </div>

      <div className="flex-1 min-h-0 overflow-y-auto">
        {content == null ? null : !hasContent ? (
          <div className="flex items-center justify-center h-full min-h-[160px] px-6 text-center">
            <p className="text-[13px]" style={{ color: C.textDim }}>
              {t("resultEmpty")}
            </p>
          </div>
        ) : (
          <>
            {/* ── Kopfzone: die Antwort, ohne zu scrollen ──────────────
                Das ist der ganze Zweck des Umbaus. Wer das Panel öffnet,
                will EINE Sache wissen: wo steht die Gruppe. */}
            <div className="px-4 pt-4 pb-3 border-b" style={{ borderColor: C.borderSubtle }}>
              <div
                className="text-[11px] font-mono uppercase tracking-wider mb-1.5"
                style={{ color: C.textMuted }}
              >
                {t("resultVerdictLabel")}
              </div>
              <p
                data-testid="result-verdict"
                className="text-[15px] leading-[1.5] font-medium"
                style={{ color: outline.lead ? C.textPrimary : C.textMuted }}
              >
                {outline.lead ?? t("resultNoVerdict")}
              </p>

              {/* Zeitleiste statt Stepper: auf dem Handy passt eine Reihe
                  antippbarer Punkte in eine Zeile, ein `‹ 2 von 3 ›` wirkt
                  dort verloren. Und man sieht auf einen Blick, WIE VIELE
                  Runden es gab — die Zahl allein sagt das nicht. */}
              {versions.length > 0 && (
                <div className="mt-3 flex items-center gap-1 flex-wrap" data-testid="result-timeline">
                  {versions.map((round) => {
                    const active = viewVersion === round;
                    return (
                      <button
                        key={round}
                        type="button"
                        onClick={() => setViewVersion(round)}
                        aria-current={active}
                        aria-label={t("resultOlderVersion", { round })}
                        className="px-2 py-0.5 rounded-md font-mono text-[11px] tabular-nums cursor-pointer transition-colors"
                        style={{
                          background: active ? C.accentSubtle : "transparent",
                          color: active ? C.accent : C.textMuted,
                          border: `1px solid ${active ? C.borderAccent : C.border}`,
                        }}
                      >
                        {t("resultRoundLabel", { round })}
                      </button>
                    );
                  })}
                  <button
                    type="button"
                    data-testid="result-newest"
                    onClick={() => setViewVersion(null)}
                    aria-current={viewVersion == null}
                    className="px-2 py-0.5 rounded-md font-mono text-[11px] cursor-pointer transition-colors"
                    style={{
                      background: viewVersion == null ? C.accentSubtle : "transparent",
                      color: viewVersion == null ? C.accent : C.textMuted,
                      border: `1px solid ${viewVersion == null ? C.borderAccent : C.border}`,
                    }}
                  >
                    {t("resultLiveLabel")}
                  </button>
                </div>
              )}
            </div>

            {/* ── Abschnitte als Einträge ──────────────────────────────
                Alle zu. Die Antwort steht oben; wer den Beleg will, öffnet
                gezielt einen Abschnitt statt an allem vorbeizuscrollen. */}
            <div data-testid="result-sections">
              {outline.sections.map((section) => {
                const open = openIds.has(section.id);
                return (
                  <div key={section.id} className="border-b" style={{ borderColor: C.borderSubtle }}>
                    <button
                      type="button"
                      onClick={() => toggleSection(section.id)}
                      aria-expanded={open}
                      aria-label={open ? t("resultSectionCollapse") : t("resultSectionExpand")}
                      data-testid="result-section-toggle"
                      className="w-full flex items-start gap-2 px-4 py-3 text-left bg-transparent border-0 cursor-pointer"
                    >
                      {open ? (
                        <ChevronDown size={14} className="shrink-0 mt-0.5" style={{ color: C.textDim }} />
                      ) : (
                        <ChevronRight size={14} className="shrink-0 mt-0.5" style={{ color: C.textDim }} />
                      )}
                      <span className="min-w-0 flex-1">
                        <span
                          className="block text-[13px] font-medium"
                          style={{ color: section.isGoal ? C.textMuted : C.textPrimary }}
                        >
                          {section.title || t("resultPanel")}
                        </span>
                        {/* Die Vorschau des Abschnitts, aus dem das Verdikt
                            stammt, wäre wortgleich mit der Kopfzone — zweimal
                            derselbe Satz übereinander liest sich als Fehler. */}
                        {!open && section.preview && section.preview !== outline.lead && (
                          <span
                            className="block truncate text-[12px] mt-0.5"
                            style={{ color: C.textMuted }}
                          >
                            {section.preview}
                          </span>
                        )}
                      </span>
                    </button>
                    {open && (
                      <div
                        data-testid="result-section-body"
                        className="px-4 pb-4 pl-10 text-[14px] leading-[1.7] max-w-[76ch] min-w-0 [&>*:last-child]:mb-0"
                      >
                        <MarkdownContent content={section.body} />
                      </div>
                    )}
                  </div>
                );
              })}
            </div>

            {/* Der Vorspann der Engine ganz unten und zugeklappt: er erklärt
                dem Lead-Agenten seine Pflichten. Für den Leser ist er einmal
                interessant und danach nie wieder — aber wegwerfen wäre
                unehrlich, also steht er hier. */}
            {outline.note && (
              <div className="px-4 py-3">
                <button
                  type="button"
                  onClick={() => setNoteOpen((v) => !v)}
                  aria-expanded={noteOpen}
                  data-testid="result-note-toggle"
                  className="flex items-center gap-1.5 bg-transparent border-0 p-0 cursor-pointer font-mono text-[11px]"
                  style={{ color: C.textMuted }}
                >
                  <Info size={12} className="shrink-0" style={{ color: C.textDim }} />
                  {t("resultNote")}
                </button>
                {noteOpen && (
                  <p
                    data-testid="result-note-body"
                    className="mt-1.5 ml-5 text-[12px] leading-[1.6]"
                    style={{ color: C.textMuted }}
                  >
                    {outline.note}
                  </p>
                )}
              </div>
            )}
          </>
        )}
      </div>

      <div
        className="flex items-center px-3 py-2 border-t shrink-0"
        style={{ borderColor: C.border }}
      >
        <button
          type="button"
          data-testid="result-copy"
          onClick={handleCopy}
          disabled={!hasContent}
          className="inline-flex items-center gap-1.5 px-2 py-1 rounded-md text-[12px] font-medium transition-colors disabled:opacity-40 cursor-pointer disabled:cursor-default"
          style={{ border: `1px solid ${C.border}`, color: C.textSecondary }}
        >
          {copied ? <Check size={12} /> : <Copy size={12} />}
          {copied ? t("resultCopied") : t("resultCopy")}
        </button>
      </div>
    </div>
  );
}
