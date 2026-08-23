"use client";

/**
 * ResultDocPanel — das lebende Ergebnis-Dokument einer Gruppe im Seiten-Panel
 * (ADR-075): aktueller Datei-Stand plus Stepper durch die Runden-Snapshots.
 *
 * Nicht offensichtlich: „neueste Version" ist bewusst KEIN Snapshot, sondern
 * die Datei selbst (`document(groupId)` ohne `version`). Ein Snapshot friert
 * den Stand am Rundenende ein, die Datei kann seither weitergeschrieben worden
 * sein. Darum ist `viewVersion === null` ein eigener Zustand und nicht einfach
 * der letzte Eintrag der Versionsliste — nur so zeigt der Sprung zurück auf
 * „Newest" wirklich das Aktuelle und nicht den letzten Gefrierpunkt.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslations } from "next-intl";
import { Check, ChevronLeft, ChevronRight, Copy } from "lucide-react";
import { api } from "@/lib/api";
import { C } from "@/lib/colors";
import { MarkdownContent } from "@/components/chat/MarkdownContent";

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

  // Gruppenwechsel: bewusst beim Rendern zurückgesetzt und nicht im Effekt.
  // Ein Effekt läuft erst NACH dem Rendern — für einen Frame stünde also das
  // Dokument der alten Gruppe unter dem Kopf der neuen, und der Lade-Effekt
  // würde zusätzlich den alten Snapshot der neuen Gruppe anfragen.
  const [renderedGroupId, setRenderedGroupId] = useState(groupId);
  if (renderedGroupId !== groupId) {
    setRenderedGroupId(groupId);
    setViewVersion(null);
    setContent(null);
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
  // Ohne dieses Einmischen würde der Stepper in genau diesem Moment eine
  // Version verlieren und wieder auftauchen lassen — ein Zappeln, das der
  // Nutzer als Fehler liest.
  const versions = useMemo(() => {
    if (latestVersion == null || snapshotRounds.includes(latestVersion)) return snapshotRounds;
    return [...snapshotRounds, latestVersion].sort((a, b) => a - b);
  }, [snapshotRounds, latestVersion]);

  const total = versions.length;
  const idx = viewVersion == null ? total - 1 : Math.max(versions.indexOf(viewVersion), 0);
  const canPrev = idx > 0;
  const canNext = total > 0 && idx < total - 1;

  // Ein eingefrorener Snapshot ändert sich nie — nur der Live-Stand wird bei
  // einer neuen Version nachgeladen.
  const liveKey = viewVersion == null ? latestVersion : null;

  useEffect(() => {
    let cancelled = false;
    const load = viewVersion == null
      ? api.groups.document(groupId)
      : api.groups.document(groupId, viewVersion);
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

  const hasContent = content != null && content.trim().length > 0;

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

  return (
    <div className="flex flex-col h-full min-h-0">
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

        {total > 0 && (
          <div className="ml-auto flex items-center gap-1">
            <button
              type="button"
              data-testid="result-prev"
              onClick={() => setViewVersion(versions[idx - 1])}
              disabled={!canPrev}
              aria-label={t("resultOlderVersion", { round: versions[Math.max(idx - 1, 0)] })}
              className="flex items-center justify-center w-6 h-6 rounded-md transition-colors disabled:opacity-30 cursor-pointer disabled:cursor-default"
              style={{ color: C.textSecondary }}
            >
              <ChevronLeft size={14} />
            </button>
            <span
              className="font-mono text-[11px] tabular-nums whitespace-nowrap"
              style={{ color: C.textSecondary }}
            >
              {t("resultVersion", { current: idx + 1, total })}
            </span>
            <button
              type="button"
              data-testid="result-next"
              onClick={() => setViewVersion(idx + 1 >= total - 1 ? null : versions[idx + 1])}
              disabled={!canNext}
              aria-label={
                idx + 1 >= total - 1
                  ? t("resultNewest")
                  : t("resultVersion", { current: idx + 2, total })
              }
              className="flex items-center justify-center w-6 h-6 rounded-md transition-colors disabled:opacity-30 cursor-pointer disabled:cursor-default"
              style={{ color: C.textSecondary }}
            >
              <ChevronRight size={14} />
            </button>
          </div>
        )}
      </div>

      {updating && (
        <div
          className="px-3 py-1 text-[11px] animate-pulse shrink-0"
          style={{ color: C.textMuted }}
          aria-live="polite"
        >
          {t("resultUpdating")}
        </div>
      )}

      {viewVersion != null && (
        // Ruhiges Band statt Warnfarbe: eine ältere Version anzuschauen ist
        // kein Fehlzustand, sondern eine Absicht.
        <div
          className="flex items-center gap-2 px-3 py-1.5 text-[12px] border-b shrink-0"
          style={{ background: C.bgElevated, borderColor: C.borderSubtle, color: C.textMuted }}
        >
          <span className="min-w-0 truncate">{t("resultOlderVersion", { round: viewVersion })}</span>
          <button
            type="button"
            data-testid="result-newest"
            onClick={() => setViewVersion(null)}
            className="ml-auto shrink-0 px-2 py-0.5 rounded-md text-[12px] font-medium transition-colors cursor-pointer"
            style={{ border: `1px solid ${C.border}`, color: C.textSecondary }}
          >
            {t("resultNewest")}
          </button>
        </div>
      )}

      <div className="flex-1 min-h-0 overflow-y-auto px-4 py-3">
        {content == null ? null : hasContent ? (
          <div className="text-[14px] leading-[1.7] max-w-[76ch] min-w-0 [&>*:last-child]:mb-0">
            <MarkdownContent content={content} />
          </div>
        ) : (
          <div className="flex items-center justify-center h-full min-h-[160px] px-6 text-center">
            <p className="text-[13px]" style={{ color: C.textDim }}>
              {t("resultEmpty")}
            </p>
          </div>
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
