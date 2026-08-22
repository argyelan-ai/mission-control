"use client";

/**
 * RoundDivider — Trennzeile zwischen zwei Runden im Gruppenraum (ADR-075).
 *
 * Bewusst NICHT in Akzentfarbe: der helle Akzent (C.accent) trägt im
 * Gruppenraum über Helligkeit und bleibt dem Ungelesen-Marker vorbehalten.
 * Eine Rundengrenze ist Gliederung, kein Signal — sie trägt über Position
 * (zentriert, mit Haarlinien) und Mono-Kleinschrift.
 */
import { useTranslations } from "next-intl";
import { C } from "@/lib/colors";

interface RoundDividerProps {
  round: number;
  /** ISO-Zeitstempel, üblicherweise der Start der Runde. */
  time?: string | null;
}

/** HH:MM, 24h, de-CH. Ein kaputter Zeitstempel darf die Trennzeile nicht
 *  killen — dann bleibt die Uhrzeit weg statt „Invalid Date" zu zeigen.
 *  (Bewusst lokal und nicht geteilt: die beiden Gruppenchat-Register sind
 *  die einzigen Nutzer, ein neues lib-Modul wäre mehr Kopplung als Nutzen.) */
function formatClock(raw: string | null | undefined): string | null {
  if (!raw) return null;
  const d = new Date(raw);
  if (Number.isNaN(d.getTime())) return null;
  return d.toLocaleTimeString("de-CH", { hour: "2-digit", minute: "2-digit", hour12: false });
}

export function RoundDivider({ round, time }: RoundDividerProps) {
  const t = useTranslations("sessions.groups");
  const label = t("roundDivider", { round });
  const clock = formatClock(time);

  return (
    // Der Screenreader hört die Haarlinien nicht — deshalb trägt die Rolle
    // „separator" den Rundennamen als Label, nicht nur die Optik.
    <div
      role="separator"
      aria-label={label}
      className="flex items-center gap-3 px-4 md:px-5 py-3 select-none"
    >
      <span className="h-px flex-1" style={{ backgroundColor: C.borderSubtle }} aria-hidden />
      <span className="flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-[0.14em] whitespace-nowrap">
        <span style={{ color: C.textMuted }}>{label}</span>
        {clock && (
          <>
            <span aria-hidden style={{ color: C.textDim }}>
              ·
            </span>
            <span style={{ color: C.textDim }}>{clock}</span>
          </>
        )}
      </span>
      <span className="h-px flex-1" style={{ backgroundColor: C.borderSubtle }} aria-hidden />
    </div>
  );
}
