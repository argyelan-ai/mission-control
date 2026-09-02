"use client";

/**
 * RoleField + RoleChip — Geräterolle (Rezept-Umschalter P2).
 *
 * Die Rolle ist NUR eine Standardvorgabe für den Zweibox-Fall: welche Box im
 * Duo per Default Head ist und welche Worker. Ein-Box-Rezepte ignorieren sie
 * vollständig — deshalb sperrt die Oberfläche nirgends etwas und die Auswahl
 * ist überall änderbar (Wizard, Formular, Pairing, Einstellungsmaske).
 *
 * Vorbelegung (`suggestRole`): die erste Box im Bestand wird Head, jede
 * weitere Worker. Das ist ein Vorschlag, sichtbar als solcher beschriftet,
 * und mit einem Klick umgedreht.
 *
 * Ein Feld, drei Wirte: das Formular (HostsSection) beschriftet mit kleinem
 * Muted-Text, der Wizard mit Mono-Uppercase-Label. Deshalb trägt das Feld
 * seine Beschriftung selbst und nimmt nur die Label-Klasse des Wirts an —
 * so sieht die Rolle in jedem Dialog aus wie dessen übrige Felder.
 */

import { useRef, type KeyboardEvent } from "react";
import { useTranslations } from "next-intl";
import { C } from "@/lib/colors";
import type { HostRole } from "@/lib/types";

/** Erste Box → Head, jede weitere → Worker. Reine Funktion, separat testbar. */
export function suggestRole(existingHostCount: number): HostRole {
  return existingHostCount === 0 ? "head" : "worker";
}

const ROLE_OPTIONS: ReadonlyArray<{ value: HostRole; labelKey: string }> = [
  { value: "head", labelKey: "roleHead" },
  { value: "worker", labelKey: "roleWorker" },
];

export function RoleField({
  value,
  onChange,
  labelClassName,
  suggested = false,
  allowNone = false,
  idPrefix = "host-role",
}: {
  value: HostRole | null;
  onChange: (role: HostRole | null) => void;
  /** Label-Klasse des Wirts (Formular: `text-xs`, Wizard: wizardLabelClass). */
  labelClassName: string;
  /** true = der Wert ist ein Vorschlag → Satz „Vorschlag: …" erscheint. */
  suggested?: boolean;
  /** Einstellungsmaske: „Keine" als dritte Wahl, damit NULL erreichbar bleibt. */
  allowNone?: boolean;
  idPrefix?: string;
}) {
  const t = useTranslations("runtimes.hosts");
  const hintId = `${idPrefix}-hint`;
  const groupRef = useRef<HTMLDivElement | null>(null);

  // ARIA-Radiogroup: genau EIN Radio im Tab-Fluss (das gewählte, sonst das
  // erste), Pfeiltasten wechseln die Option und tragen den Fokus mit.
  const values: Array<HostRole | null> = [
    ...ROLE_OPTIONS.map((o) => o.value),
    ...(allowNone ? [null] : []),
  ];
  const currentIdx = values.indexOf(value);
  const tabStopIdx = currentIdx >= 0 ? currentIdx : 0;
  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    const dir =
      e.key === "ArrowRight" || e.key === "ArrowDown" ? 1
      : e.key === "ArrowLeft" || e.key === "ArrowUp" ? -1
      : 0;
    if (dir === 0) return;
    e.preventDefault();
    const base = currentIdx >= 0 ? currentIdx : (dir > 0 ? -1 : 0);
    const next = (base + dir + values.length) % values.length;
    onChange(values[next]);
    const radios = groupRef.current?.querySelectorAll<HTMLButtonElement>('[role="radio"]');
    radios?.[next]?.focus();
  };
  const chipClass =
    "text-xs px-2.5 py-1.5 sm:py-1 min-h-11 sm:min-h-0 rounded-md cursor-pointer transition-colors font-mono uppercase tracking-wider";
  const chipStyle = (active: boolean) => ({
    background: active ? C.accentSubtle : C.borderSubtle,
    border: `1px solid ${active ? C.borderAccent : C.border}`,
    color: active ? C.accent : C.textMuted,
    fontWeight: active ? 600 : 500,
  });

  return (
    <div className="flex flex-col gap-1">
      <span id={`${idPrefix}-label`} className={labelClassName}>
        {t("fieldRole")}
      </span>
      <div
        ref={groupRef}
        role="radiogroup"
        aria-labelledby={`${idPrefix}-label`}
        aria-describedby={hintId}
        onKeyDown={onKeyDown}
        className="flex gap-1.5"
      >
        {ROLE_OPTIONS.map((opt, i) => {
          const active = value === opt.value;
          return (
            <button
              key={opt.value}
              type="button"
              role="radio"
              aria-checked={active}
              tabIndex={tabStopIdx === i ? 0 : -1}
              data-testid={`${idPrefix}-${opt.value}`}
              onClick={() => onChange(opt.value)}
              className={chipClass}
              style={chipStyle(active)}
            >
              {t(opt.labelKey)}
            </button>
          );
        })}
        {allowNone && (
          <button
            type="button"
            role="radio"
            aria-checked={value === null}
            tabIndex={tabStopIdx === ROLE_OPTIONS.length ? 0 : -1}
            data-testid={`${idPrefix}-none`}
            onClick={() => onChange(null)}
            className={chipClass}
            style={chipStyle(value === null)}
          >
            {t("roleNone")}
          </button>
        )}
      </div>
      <p id={hintId} className="text-[11px] leading-relaxed" style={{ color: C.textMuted }}>
        {t("roleHint")}
        {suggested && (
          <>
            {" "}
            <span data-testid={`${idPrefix}-suggested`} style={{ color: C.textSecondary }}>
              {t("roleSuggested")}
            </span>
          </>
        )}
      </p>
    </div>
  );
}

/**
 * Kleiner Mono-Chip `HEAD` / `WORKER` für die Kopfzeile der Gerätekachel.
 * Rendert NICHTS ohne Rolle — die Kachel bleibt unverändert, wenn keine
 * gesetzt ist (Vertrag: „nur wenn gesetzt").
 */
export function RoleChip({ role }: { role: HostRole | null | undefined }) {
  const t = useTranslations("runtimes.hosts");
  if (!role) return null;
  return (
    <span
      data-testid="host-role-chip"
      data-role={role}
      className="text-[9px] px-1.5 py-0.5 rounded-sm font-mono uppercase tracking-wide shrink-0"
      style={{ background: C.accentSubtle, color: C.accent, border: `1px solid ${C.borderAccent}` }}
      title={t("roleHint")}
    >
      {role === "head" ? t("roleHead") : t("roleWorker")}
    </span>
  );
}
