"use client";

/**
 * Ein Anhang im Chat-Verlauf — Bild als echte Vorschau, alles andere als Karte.
 *
 * Die Datei liegt als Agenten-Referenz unter `~/.mc/references/agent/<id>/…`;
 * der Files-Root
 * "references" ist browsable, also kann das Frontend sie über den
 * bestehenden Content-Endpunkt holen (mit Bearer-Header, darum `useAuthBlob`).
 * Es gibt bewusst KEINEN eigenen Ausliefer-Endpunkt für Anhänge — ein zweiter
 * Weg zu denselben Bytes wäre eine zweite Stelle, an der die Rechte stimmen
 * müssten.
 *
 * Nicht-Bilder bekommen absichtlich keine Vorschau: Ein Video oder eine
 * 20-MB-PDF im Verlauf zu laden kostet Bandbreite für etwas, das niemand
 * angefordert hat. Die Karte nennt den Namen und öffnet die Datei auf Klick.
 */
import { useState } from "react";
import { useTranslations } from "next-intl";
import { FileText, ImageOff } from "lucide-react";
import { api } from "@/lib/api";
import { useAuthBlob } from "@/hooks/useAuthBlob";
import { C } from "@/lib/colors";
import type { ParsedAttachmentRef } from "./attachments";

/** Wo der Files-Endpunkt den Anhang findet.
 *
 *  Der Regelfall ist keine Rechnung: Der Anhang-Endpunkt gibt `root` und
 *  `subpath` selbst zurück, und der Composer reicht sie durch — das ist die
 *  Auskunft der Ablage, nicht eine Vermutung über sie.
 *
 *  Zurückgerechnet wird nur, was aus dem TRANSKRIPT stammt: Dort steht
 *  ausschliesslich der absolute Pfad, den der Agent gesehen hat (mehr gibt es
 *  in einem CLI-Transkript nicht, siehe `attachments.ts`). Liegt der Pfad
 *  nicht im references-Root, wird `null` zurückgegeben — dann lädt die Kachel
 *  nichts und behauptet nichts, statt eine Adresse zu raten. */
export function toFilesRef(
  att: Pick<ParsedAttachmentRef, "path" | "root" | "subpath">,
): { root: string; subpath: string } | null {
  if (att.root && att.subpath) return { root: att.root, subpath: att.subpath };
  const marker = "/.mc/references/";
  const at = att.path.indexOf(marker);
  if (at === -1) return null;
  const subpath = att.path.slice(at + marker.length);
  return subpath ? { root: "references", subpath } : null;
}

export function ChatAttachmentTile({ att }: { att: ParsedAttachmentRef }) {
  const t = useTranslations("sessions");
  const ref = toFilesRef(att);
  const url = ref ? api.files.contentUrl(ref.root, ref.subpath) : null;
  const { blobUrl, error } = useAuthBlob(att.isImage && url ? url : null);
  const [expanded, setExpanded] = useState(false);

  if (att.isImage && blobUrl) {
    return (
      <button
        type="button"
        data-testid="attachment-image"
        onClick={() => setExpanded((v) => !v)}
        aria-label={
          expanded
            ? t("attachmentCollapseImage", { name: att.name })
            : t("attachmentExpandImage", { name: att.name })
        }
        className="block rounded-lg overflow-hidden cursor-pointer"
        style={{ border: `1px solid ${C.border}` }}
      >
        {/* eslint-disable-next-line @next/next/no-img-element -- blob:-URL, kein
            Kandidat für die Bild-Optimierung von Next */}
        <img
          src={blobUrl}
          alt={att.name}
          className="block object-contain"
          style={{ maxHeight: expanded ? 480 : 160, maxWidth: "100%" }}
        />
      </button>
    );
  }

  // Bild, das sich nicht laden liess: ehrlich benennen statt einen leeren
  // Rahmen zu zeigen. Meist wurde der Agent samt seinen Referenzen gelöscht.
  const Icon = att.isImage && error ? ImageOff : FileText;
  const hint = att.isImage && error ? t("attachmentUnavailable") : null;

  return (
    <a
      data-testid="attachment-card"
      href={url ?? undefined}
      target="_blank"
      rel="noreferrer"
      className="inline-flex items-center gap-2 px-2.5 py-1.5 rounded-lg max-w-full"
      style={{ backgroundColor: C.bgHover, border: `1px solid ${C.border}` }}
    >
      <Icon size={14} className="shrink-0" style={{ color: C.textMuted }} />
      <span className="text-[12px] truncate min-w-0" style={{ color: C.textPrimary }}>
        {att.name}
      </span>
      {hint && (
        <span className="text-[11px] shrink-0" style={{ color: C.textMuted }}>
          {hint}
        </span>
      )}
    </a>
  );
}
