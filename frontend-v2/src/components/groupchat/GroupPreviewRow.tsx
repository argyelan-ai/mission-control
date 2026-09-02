"use client";

import { useTranslations } from "next-intl";
import { Radio } from "lucide-react";
import { C } from "@/lib/colors";
import { EntityIcon } from "@/components/shared/EntityIcon";

/**
 * GroupPreviewRow — was ein Mitglied gerade im Terminal tippt, bevor sein
 * Beitrag im Raum steht (Live-Schicht A, Gruppen-Ausgabe; Quelle ist derselbe
 * Vorschau-Strom wie im Sessions-Chat, siehe `group.preview`).
 *
 * Sitzt in der Sprecher-Rinne wie ein echter Beitrag (Avatar links, Name
 * oben), damit klar ist, WER schreibt — ist aber unverwechselbar als Vorschau
 * gekennzeichnet: pulsierender Punkt, „tippt …", Monospace und gedämpfte
 * Farbe. Roher Bildschirmtext, kein Markdown (halbe Sätze, Tool-Zeilen).
 */
export function GroupPreviewRow({
  name,
  emoji,
  text,
}: {
  name: string | null;
  emoji: string | null;
  text: string;
}) {
  const t = useTranslations("sessions.groups");
  return (
    <div className="w-full px-4 md:px-5 pt-2 pb-1.5" data-testid="group-preview-row" aria-live="polite">
      <div className="flex gap-2.5">
        <div className="w-7 shrink-0 flex justify-center pt-[3px]">
          <EntityIcon value={emoji} size={18} style={{ color: C.textMuted }} />
        </div>
        <div className="min-w-0 flex-1">
          <div className="mb-1 flex items-baseline gap-2">
            {name && (
              <span className="font-mono text-[11px] font-medium" style={{ color: C.textSecondary }}>
                {name}
              </span>
            )}
            <span className="label-sys flex items-center gap-1.5" style={{ color: C.textMuted }}>
              <Radio size={11} className="animate-pulse" aria-hidden="true" />
              <span>{t("typingPreview")}</span>
            </span>
          </div>
          <pre
            className="font-mono text-[12.5px] leading-[1.55] max-w-[76ch] min-w-0 whitespace-pre-wrap break-words m-0"
            style={{ color: C.textSecondary }}
          >
            {text}
          </pre>
        </div>
      </div>
    </div>
  );
}
