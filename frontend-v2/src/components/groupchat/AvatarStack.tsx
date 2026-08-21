"use client";

/**
 * AvatarStack — überlappende Mitglieder-Avatare einer Gruppe (Discord-Muster).
 *
 * Streng achromatisch: kein Mitglied bekommt eine eigene Farbe, die Runde wird
 * allein über Icon + Name unterscheidbar. Nicht offensichtlich: <EntityIcon>
 * setzt `aria-hidden`, der Stapel wäre für Screenreader also komplett stumm —
 * deshalb trägt der Container role="img" und listet ALLE Namen im Label, auch
 * die, die hinter dem „+N"-Kreis stecken.
 */

import { useTranslations } from "next-intl";
import { C } from "@/lib/colors";
import { EntityIcon } from "@/components/shared/EntityIcon";

interface AvatarStackProps {
  members: { id: string; emoji: string | null; name: string }[];
  /** Wie viele Kreise voll ausgezeichnet werden; der Rest wandert in „+N". */
  max?: number;
  /** Kantenlänge eines Kreises in px. */
  size?: number;
}

export function AvatarStack({ members, max = 3, size = 20 }: AvatarStackProps) {
  const t = useTranslations("sessions.groups");

  // Kein Mitglied = kein Platzhalter: eine leere Gruppe soll die Zeile nicht
  // um einen sinnlosen Kreis breiter machen.
  if (members.length === 0) return null;

  const visible = members.slice(0, max);
  const rest = members.length - visible.length;

  const circle: React.CSSProperties = {
    width: size,
    height: size,
    background: C.bgElevated,
    border: `1px solid ${C.border}`,
    color: C.textSecondary,
  };

  return (
    <span
      role="img"
      aria-label={members.map((m) => m.name).join(", ")}
      className="flex items-center shrink-0"
    >
      {visible.map((m, i) => (
        <span
          key={m.id}
          data-testid="avatar-stack-item"
          className={`flex items-center justify-center rounded-full ${i > 0 ? "-ml-2" : ""}`}
          style={circle}
        >
          <EntityIcon value={m.emoji} size={Math.round(size * 0.7)} />
        </span>
      ))}
      {rest > 0 && (
        <span
          data-testid="avatar-stack-more"
          className="flex items-center justify-center rounded-full -ml-2 font-mono leading-none"
          style={{
            ...circle,
            color: C.textMuted,
            // Skaliert mit der Kreisgröße, aber nie unter die Lesbarkeitsgrenze.
            fontSize: Math.max(9, Math.round(size * 0.45)),
          }}
        >
          {t("membersMore", { count: rest })}
        </span>
      )}
    </span>
  );
}
