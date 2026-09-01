"use client";

import { useTranslations } from "next-intl";
import { Radio } from "lucide-react";
import { C } from "@/lib/colors";
import type { PreviewEvent } from "@/lib/chatTypes";

/**
 * Die Live-Vorschau: was die CLI gerade auf ihren Bildschirm schreibt, bevor
 * die Antwort im Transkript steht (Live-Schicht A, PR #365).
 *
 * Sie sitzt dort, wo gleich die Antwort stehen wird — linksbuendig, in der
 * Lesebreite des Agenten — ist aber sichtbar als Vorschau gekennzeichnet:
 * Etikett mit pulsierendem Punkt, Monospace, gedaempfte Farbe. Der Text ist
 * ROHER Bildschirmtext (kein Markdown): die CLI zeichnet halbe Saetze,
 * Spinner-Reste und Tool-Zeilen, die als Markdown gelesen Unsinn ergaeben.
 * Was hier steht, hat das Transkript noch nicht bestaetigt — darum nie
 * verwechselbar mit einer echten Antwort.
 */
export function PreviewRow({ preview }: { preview: PreviewEvent }) {
  const t = useTranslations("sessions");
  return (
    <div className="w-full px-4 md:px-5 py-3 md:py-4" data-testid="preview-row" aria-live="polite">
      <div className="label-sys mb-2 flex items-center gap-1.5" style={{ color: C.textMuted }}>
        <Radio size={11} className="animate-pulse" aria-hidden="true" />
        <span>{t("livePreview")}</span>
      </div>
      <pre
        className="font-mono text-[12.5px] leading-[1.55] max-w-[76ch] min-w-0 whitespace-pre-wrap break-words m-0"
        style={{ color: C.textSecondary }}
      >
        {preview.text}
      </pre>
    </div>
  );
}
