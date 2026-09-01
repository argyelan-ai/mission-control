"use client";

/**
 * Geräte-Steuerung — GPU-Modus, eingebaut in die Host-Zeile
 * (docs/plans/2026-09-01-geraete-steuerung-vertrag.md, Gewerk C).
 *
 * WARUM in der Slot-Bühne:
 * Die Geräte-Ansicht ist der Reiter „fleet" — dort stehen die Boxen, dort
 * schaut der Operator hin. Die Host-Liste im Reiter „infra" ist Verwaltung
 * (anlegen, koppeln, löschen); ein Betriebs-Schalter war dort zweimal am
 * falschen Ort. Der Schalter sitzt jetzt in der Kachel der Box, deren Takt
 * er stellt — neben deren Live-Werten.
 *
 * WO GENAU, und wo NICHT:
 * In der Slot-Kachel (Box mit Runtimes) und in der Worker-Kachel (gekoppelte
 * Box ohne eigene Runtime). NICHT bei einer schlafenden oder nicht
 * erreichbaren Box und nicht bei einer, die MC gar nicht steuern kann —
 * ein Schalter, der nichts bewirken kann, ist eine Lüge.
 *
 * HONESTY RULE (Dateikopf von SlotStage.tsx): nur echte Felder. Die vier
 * Referenzwerte je Stufe sind KEINE Live-Messung dieser Box — sie stehen
 * darum mit „≈" und unter der Überschrift „gemessen", während die echten
 * Live-Werte daneben in der Telemetrie-Spalte stehen.
 *
 * WARUM zwei Ausbaustufen:
 * In einer Zeile ist der Platz knapp. Eingeklappt trägt der Schalter genau so
 * viel, wie die Wahl verlangt — die vier Stufen, was jede an Strom kostet, und
 * den Satz, dass keine davon langsamer schreibt. Wer die Messung sehen will,
 * klappt auf: dort steht das volle Diagramm, in dem die Reihe „Erzeugung"
 * schnurgerade steht, während „Strom" als Treppe fällt. Man sieht die Aussage,
 * bevor man sie liest.
 *
 * WARUM Soll und Ist beide sichtbar sind:
 * MC schickt keinen Befehl, sondern legt einen Soll-Zustand ab, den das Gerät
 * beim nächsten Heartbeat (bis zu 15 s) abholt. Der gefüllte Reiter zeigt den
 * Ist-Zustand, die gestrichelte Umrandung das Ziel. Das ist kein Fehler,
 * sondern Wartezeit — deshalb Info-Blau, nie Rot.
 *
 * ANIMATION: ausschliesslich `transform` und `opacity`. `width`/`height` zu
 * animieren erzwingt bei jedem Bild ein neues Layout und ruckelt sichtbar.
 */

import { useEffect, useState } from "react";
import { motion, useReducedMotion } from "framer-motion";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, ChevronDown, Loader2, Lock } from "lucide-react";
import { useLocale, useTranslations } from "next-intl";
import { api } from "@/lib/api";
import {
  GPU_MODES,
  type DesiredState,
  type Device,
  type DeviceStatus,
  type DeviceStatusReason,
  type GpuMode,
} from "@/lib/types";
import { C, STATUS_TEXT } from "@/lib/colors";
import type { Tone } from "@/components/shared/ListRow";

// ── Gemessene Werte ───────────────────────────────────────────────────────────
// Marks Messung 16.08.2026, Qwen38-27B auf einer Box (Vertrag, Abschnitt
// „GPU-Modi"). Fest verdrahtet und nicht aus der Telemetrie gerechnet: es ist
// eine Referenzmessung, die erklären soll, was die Wahl kostet — keine
// Live-Zahl. Live-Werte stehen daneben in der Zeile.
//
// ANNAHME (im Bericht gekennzeichnet): der Vertrag nennt „+7 % bei eco,
// +11 % bei eco+" ohne Bezugsgrösse. Wir lesen normal/boost als Grundwert.
export interface ModeFacts {
  clockMhz: number | null; // null = frei (nvidia-smi -rgc)
  tokensPerSec: number;
  watt: number;
  tempC: number;
  /** Aufschlag beim Einlesen gegenüber normal/boost, in Prozent. */
  prefillPenaltyPct: number;
  /** boost drosselt unter Dauerlast und kann hart abschalten. */
  risky?: boolean;
}

export const MODE_FACTS: Record<GpuMode, ModeFacts> = {
  "eco+": { clockMhz: 1800, tokensPerSec: 19.8, watt: 27.1, tempC: 69, prefillPenaltyPct: 11 },
  eco: { clockMhz: 2000, tokensPerSec: 20.4, watt: 32.5, tempC: 74, prefillPenaltyPct: 7 },
  normal: { clockMhz: 2200, tokensPerSec: 19.6, watt: 39.9, tempC: 81, prefillPenaltyPct: 0 },
  boost: { clockMhz: null, tokensPerSec: 20.3, watt: 59.5, tempC: 87, prefillPenaltyPct: 0, risky: true },
};

// Reihen-Skalen. Jede Reihe wächst von 0 bis zu ihrer eigenen Obergrenze —
// dieselbe Regel für alle drei, damit keine Reihe künstlich dramatisch wird.
const ROW_MAX = { tokens: 25, watt: 65, temp: 95 } as const;

const MODE_LABEL_KEY: Record<GpuMode, string> = {
  "eco+": "modeEcoPlus",
  eco: "modeEco",
  normal: "modeNormal",
  boost: "modeBoost",
};

// Ampel — Farben und Texte kommen aus dem Status, den das Backend rechnet
// (services/device_state.py). Hier wird nichts nachgerechnet.
/** Ampel → Ton der Host-Zeile, damit der Punkt links den Gerätezustand trägt. */
export const STATUS_ROW_TONE: Record<DeviceStatus, Tone> = {
  green: "ok",
  yellow: "warn",
  red: "error",
  grey: "idle",
};

const REASON_LABEL_KEY: Record<DeviceStatusReason, string> = {
  no_agent: "reasonNoAgent",
  last_error: "reasonLastError",
  no_device_state: "reasonNoDeviceState",
  pending: "reasonPending",
  stale: "reasonStale",
  in_sync: "reasonInSync",
};

/** Das Gerät meldet sich alle 15 s — so lange darf „wird übernommen" stehen. */
const HEARTBEAT_SECONDS = 15;

// ── Schalter-Ehrlichkeit (Review M7) ─────────────────────────────────────────
// Der Schalter darf nur bedienbar sein, wenn ein Klick auch etwas bewirken
// kann. In drei Zuständen ist er sichtbar, aber wirkungslos — dann wird er
// GESPERRT (nicht versteckt: der Operator soll sehen, dass es ihn gibt) und
// ein Satz sagt, was fehlt:
//   no_device_state  alter Agent, der keinen Zustand meldet → Ampel rot, ein
//                    Klick stünde für immer auf „wird übernommen"
//   stale            Agent meldet sich nicht mehr; `reachable` der Host-
//                    Metrik fällt erst nach 60 s, die Ampel schon ab 120 s —
//                    dazwischen wäre der Schalter ein leeres Versprechen
//   gpu_mode unknown Agent läuft, aber die Steuer-Skripte fehlen auf der Box
//                    → currentMode=null, ein Soll würde nie erfüllt
// Die `reason`-Werte kommen fertig vom Backend (services/device_state.py);
// hier wird nichts nachgerechnet.
export type SwitchLock = "no_device_state" | "stale" | "unknown_mode";

export function switchLockFor(device: Device): SwitchLock | null {
  if (device.reason === "no_device_state") return "no_device_state";
  if (device.reason === "stale") return "stale";
  if (device.device_state?.gpu_mode === "unknown") return "unknown_mode";
  return null;
}

/** Alter der letzten Meldung als kurzer Text: Sekunden, ab 10 Minuten Minuten. */
function ageLabel(locale: string, ageS: number): string {
  if (ageS >= 600) return `${Math.round(ageS / 60).toLocaleString(locale)} min`;
  return `${Math.round(ageS).toLocaleString(locale)} s`;
}

// Feste Zeilenhöhen der vollen Ansicht: die Beschriftungsspalte links muss auf
// den Pixel mit den Balkenreihen rechts fluchten, sonst zerfällt das Diagramm
// in zwei Listen.
const H = { head: 30, row: 80, foot: 26 } as const;

// Höhe der Bezugslinie über der Erzeugungs-Reihe — aus den Messwerten
// gerechnet, damit sie nicht lügen kann. `- 8` = paddingBottom der Reihe.
const GEN_BAR_AREA = H.row - 8;
const GEN_MEAN_PCT =
  (Object.values(MODE_FACTS).reduce((a, f) => a + f.tokensPerSec, 0) /
    Object.keys(MODE_FACTS).length /
    ROW_MAX.tokens) *
  100;
const GEN_REFERENCE_TOP = H.head + GEN_BAR_AREA * (1 - GEN_MEAN_PCT / 100);

// ── kleine Bausteine ─────────────────────────────────────────────────────────

function num(locale: string, value: number, digits = 1): string {
  return value.toLocaleString(locale, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

/** Live-Werte des Geräts als eine Zeile — für die Kopfzeile der Host-Zeile. */
export function deviceLiveSummary(
  device: Device,
  locale: string,
  units: { watt: string; temp: string },
): string | null {
  const s = device.device_state;
  if (!s) return null;
  const parts = [
    s.gpu_clock_mhz != null ? `${s.gpu_clock_mhz} MHz` : null,
    s.gpu_power_w != null ? `${num(locale, s.gpu_power_w)} ${units.watt}` : null,
    s.gpu_temp_c != null ? `${s.gpu_temp_c} ${units.temp}` : null,
  ].filter(Boolean);
  return parts.length > 0 ? parts.join(" · ") : null;
}

// ── Volle Ansicht: Schalter und Diagramm in einem Objekt ─────────────────────

function Bar({
  pct,
  active,
  tone,
  flat,
  testId,
}: {
  pct: number;
  active: boolean;
  /** "speed" = die Reihe, die sich NICHT ändert — sie trägt den Akzent. */
  tone: "speed" | "cost";
  /** Reduzierte Bewegung: Zustand ohne Übergang setzen. */
  flat: boolean;
  testId: string;
}) {
  // Skalieren statt Höhe animieren: `height` erzwingt bei JEDEM Bild ein neues
  // Layout und ruckelt. `transform` läuft auf der Grafikkarte. Der Balken ist
  // darum immer voll hoch und wird von der Grundlinie aus zusammengedrückt.
  const scale = Math.min(Math.max(pct, 0), 100) / 100;
  const background =
    tone === "speed"
      ? active
        ? C.accent
        : C.accentDeep
      : active
        ? C.textSecondary
        : C.textDim;
  return (
    // Grundlinie: die Balken stehen sichtbar auf einem Boden, sonst schweben
    // sie und die Höhen lassen sich nicht vergleichen.
    <div
      className="flex-1 flex items-end justify-center"
      style={{ height: "100%", borderBottom: `1px solid ${C.borderActive}` }}
    >
      <motion.div
        data-testid={testId}
        className="rounded-t-[3px]"
        // Nicht die volle Spaltenbreite: sonst liest man Blöcke, keine Balken.
        style={{ width: "34%", height: "100%", transformOrigin: "bottom" }}
        initial={false}
        animate={{ scaleY: scale, background }}
        transition={flat ? { duration: 0 } : { duration: 0.4, ease: [0.16, 1, 0.3, 1] }}
      />
    </div>
  );
}

/** Reihen-Beschriftung. Sitzt auf der Grundlinie, damit Name und Balkenfuss
 *  auf einer Höhe liegen — sonst schwebt die Beschriftung in der Mitte und
 *  gehört optisch zu keiner Reihe. */
function RowLabel({ name, unit }: { name: string; unit: string }) {
  return (
    <div
      className="flex flex-col justify-end"
      style={{ height: H.row, paddingBottom: 8, lineHeight: 1.25 }}
    >
      <span className="text-[11px]" style={{ color: C.textMuted }}>{name}</span>
      <span className="text-[9px]" style={{ color: C.textDim }}>{unit}</span>
    </div>
  );
}

function ModeScale({
  current,
  target,
  pending,
  disabled,
  onPick,
}: {
  /** Ist-Zustand vom Gerät. null = das Gerät hat noch nichts gemeldet. */
  current: GpuMode | null;
  /** Soll-Zustand (optimistisch), falls einer gesetzt ist. */
  target: GpuMode | null;
  pending: boolean;
  disabled: boolean;
  onPick: (mode: GpuMode) => void;
}) {
  const t = useTranslations("runtimes.devices");
  const reduce = useReducedMotion();
  const flat = !!reduce;

  // Der gefüllte Reiter markiert IMMER den Ist-Zustand. Steht kein Ist fest,
  // legt er sich unter das Ziel. Ist BEIDES offen, wird gar nichts hervor-
  // gehoben — ein Reiter auf "eco" würde eine Einstellung behaupten, die
  // niemand gemacht hat.
  const selected = current ?? target;
  const solidIndex = selected ? GPU_MODES.indexOf(selected) : -1;
  const targetIndex = target ? GPU_MODES.indexOf(target) : -1;
  const showTarget = pending && targetIndex >= 0 && targetIndex !== solidIndex;

  const highlighted = showTarget ? targetIndex : solidIndex;

  return (
    <div>
      {/* Skalen-Beschriftung: kühl/sparsam links, heiss/schnell rechts. */}
      <div
        className="flex items-center justify-between mb-2 text-[10px] uppercase"
        style={{ letterSpacing: "0.06em", color: C.textDim }}
      >
        <span>{t("scaleLow")}</span>
        <span
          aria-hidden
          className="flex-1 mx-3"
          style={{ height: "1px", background: `linear-gradient(90deg, ${C.border}, ${C.borderActive})` }}
        />
        <span>{t("scaleHigh")}</span>
      </div>

      <div className="flex">
        {/* Beschriftungsspalte — gleiche Höhen wie die Balkenreihen. */}
        <div className="shrink-0 w-[64px] sm:w-[76px] flex flex-col">
          <div style={{ height: H.head }} />
          <RowLabel name={t("rowGeneration")} unit={t("unitTokens")} />
          <RowLabel name={t("rowPower")} unit={t("unitWatt")} />
          <RowLabel name={t("rowHeat")} unit={t("unitTemp")} />
          <div
            className="flex items-center text-[10px]"
            style={{ height: H.foot, color: C.textDim }}
          >
            {t("rowPrefill")}
          </div>
        </div>

        {/* Spaltenfeld mit gleitendem Reiter darunter. */}
        <div className="relative flex-1" role="radiogroup" aria-label={t("modeGroupLabel")}>
          {solidIndex >= 0 && (
            <motion.div
              data-testid="mode-indicator"
              aria-hidden
              className="absolute top-0 bottom-0 rounded-lg pointer-events-none"
              style={{
                width: "25%",
                left: 0,
                background: C.bgElevated,
                border: `1px solid ${C.borderActive}`,
              }}
              initial={false}
              animate={{ x: `${solidIndex * 100}%` }}
              transition={flat ? { duration: 0 } : { type: "spring", stiffness: 420, damping: 34 }}
            />
          )}

          {/* Gestrichelte Umrandung = Ziel, solange das Gerät nachzieht.
              Atmet langsam — Warten, kein Fehler. */}
          {showTarget && (
            <motion.div
              data-testid="mode-target-outline"
              aria-hidden
              className="absolute top-0 bottom-0 rounded-lg pointer-events-none"
              style={{ width: "25%", left: 0, border: `1px dashed ${C.info}`, background: `${C.info}0F` }}
              initial={false}
              animate={
                flat
                  ? { x: `${targetIndex * 100}%`, opacity: 1 }
                  : { x: `${targetIndex * 100}%`, opacity: [0.45, 1, 0.45] }
              }
              transition={
                flat
                  ? { duration: 0 }
                  : {
                      x: { type: "spring", stiffness: 420, damping: 34 },
                      opacity: { duration: 2.2, repeat: Infinity, ease: "easeInOut" },
                    }
              }
            />
          )}

          {/* Bezugslinie über der Erzeugungs-Reihe: sie berührt alle vier
              Balken. Aus den Messwerten gerechnet, nicht gesetzt — wären die
              Stufen verschieden schnell, ginge die Linie sichtbar daneben. */}
          <div
            aria-hidden
            className="absolute pointer-events-none"
            style={{ left: 0, right: 0, top: GEN_REFERENCE_TOP, borderTop: `1px dashed ${C.borderAccent}` }}
          />

          <div className="relative grid grid-cols-4">
            {GPU_MODES.map((mode, i) => {
              const f = MODE_FACTS[mode];
              const active = i === highlighted;
              return (
                <button
                  key={mode}
                  type="button"
                  role="radio"
                  aria-checked={current === mode}
                  aria-label={t(MODE_LABEL_KEY[mode])}
                  disabled={disabled}
                  onClick={() => onPick(mode)}
                  data-testid={`mode-${mode}`}
                  data-active={active ? "true" : "false"}
                  className="flex flex-col text-center rounded-lg cursor-pointer disabled:cursor-not-allowed focus-visible:outline-none focus-visible:ring-1"
                  style={{ ["--tw-ring-color" as string]: C.borderAccent }}
                >
                  <span
                    className="flex items-center justify-center gap-1 text-[12px]"
                    style={{
                      height: H.head,
                      fontWeight: active ? 600 : 500,
                      color: active ? C.textPrimary : C.textMuted,
                    }}
                  >
                    {f.risky && (
                      <AlertTriangle
                        size={10}
                        aria-hidden
                        style={{ color: active ? STATUS_TEXT.warning : C.textDim }}
                      />
                    )}
                    {t(MODE_LABEL_KEY[mode])}
                  </span>

                  <span className="flex" style={{ height: H.row, paddingBottom: 8 }}>
                    <Bar testId={`bar-generation-${mode}`} pct={(f.tokensPerSec / ROW_MAX.tokens) * 100} active={active} tone="speed" flat={flat} />
                  </span>
                  <span className="flex" style={{ height: H.row, paddingBottom: 8 }}>
                    <Bar testId={`bar-power-${mode}`} pct={(f.watt / ROW_MAX.watt) * 100} active={active} tone="cost" flat={flat} />
                  </span>
                  <span className="flex" style={{ height: H.row, paddingBottom: 8 }}>
                    <Bar testId={`bar-heat-${mode}`} pct={(f.tempC / ROW_MAX.temp) * 100} active={active} tone="cost" flat={flat} />
                  </span>

                  <span
                    className="flex items-center justify-center tabular-nums text-[10px]"
                    style={{
                      height: H.foot,
                      color: f.prefillPenaltyPct > 0 ? C.textMuted : C.textDim,
                    }}
                  >
                    {f.prefillPenaltyPct > 0 ? `+${f.prefillPenaltyPct} %` : "±0 %"}
                  </span>
                </button>
              );
            })}
          </div>
        </div>
      </div>
    </div>
  );
}

function FactsReadout({ mode }: { mode: GpuMode }) {
  const t = useTranslations("runtimes.devices");
  const locale = useLocale();
  const f = MODE_FACTS[mode];
  const items = [
    { k: "factGeneration", v: `${num(locale, f.tokensPerSec)} ${t("unitTokens")}` },
    { k: "factPrefill", v: f.prefillPenaltyPct > 0 ? `+${f.prefillPenaltyPct} %` : `±0 %` },
    { k: "factPower", v: `${num(locale, f.watt)} ${t("unitWatt")}` },
    { k: "factHeat", v: `${f.tempC} ${t("unitTemp")}` },
  ];
  return (
    <div className="grid grid-cols-2 sm:grid-cols-4 gap-x-4 gap-y-2 mt-3">
      {items.map((it) => (
        <div key={it.k} className="flex flex-col">
          <span className="text-[10px]" style={{ color: C.textDim }}>{t(it.k)}</span>
          <span className="tabular-nums text-[15px] font-semibold" style={{ color: C.textPrimary }}>
            {it.v}
          </span>
        </div>
      ))}
    </div>
  );
}

// ── Kompakte Ansicht: der Schalter, wie er in der Zeile steht ────────────────

/**
 * Vier Segmente mit dem Stromwert je Stufe und einem Balken, dessen Länge den
 * Verbrauch trägt. Die Treppe von links nach rechts ist die halbe Botschaft;
 * die andere Hälfte steht als Satz darunter, weil sie sich auf dieser Fläche
 * nicht zeichnen lässt (gleich hohe Balken sähen aus wie ein Zeichenfehler).
 */
function CompactModeSwitch({
  current,
  target,
  pending,
  disabled,
  onPick,
}: {
  current: GpuMode | null;
  target: GpuMode | null;
  pending: boolean;
  disabled: boolean;
  onPick: (mode: GpuMode) => void;
}) {
  const t = useTranslations("runtimes.devices");
  const locale = useLocale();
  const reduce = useReducedMotion();
  const flat = !!reduce;

  const selected = current ?? target;
  const solidIndex = selected ? GPU_MODES.indexOf(selected) : -1;
  const targetIndex = target ? GPU_MODES.indexOf(target) : -1;
  const showTarget = pending && targetIndex >= 0 && targetIndex !== solidIndex;
  const highlighted = showTarget ? targetIndex : solidIndex;

  return (
    <div
      className="relative rounded-lg overflow-hidden"
      style={{ background: C.bgBase, border: `1px solid ${C.border}` }}
      role="radiogroup"
      aria-label={t("modeGroupLabel")}
    >
      {solidIndex >= 0 && (
        <motion.div
          data-testid="compact-indicator"
          aria-hidden
          className="absolute top-0 bottom-0 rounded-md pointer-events-none"
          style={{
            width: "25%",
            left: 0,
            background: C.bgElevated,
            border: `1px solid ${C.borderActive}`,
          }}
          initial={false}
          animate={{ x: `${solidIndex * 100}%` }}
          transition={flat ? { duration: 0 } : { type: "spring", stiffness: 420, damping: 34 }}
        />
      )}

      {showTarget && (
        <motion.div
          data-testid="compact-target-outline"
          aria-hidden
          className="absolute top-0 bottom-0 rounded-md pointer-events-none"
          style={{ width: "25%", left: 0, border: `1px dashed ${C.info}`, background: `${C.info}0F` }}
          initial={false}
          animate={
            flat
              ? { x: `${targetIndex * 100}%`, opacity: 1 }
              : { x: `${targetIndex * 100}%`, opacity: [0.45, 1, 0.45] }
          }
          transition={
            flat
              ? { duration: 0 }
              : {
                  x: { type: "spring", stiffness: 420, damping: 34 },
                  opacity: { duration: 2.2, repeat: Infinity, ease: "easeInOut" },
                }
          }
        />
      )}

      <div className="relative grid grid-cols-4">
        {GPU_MODES.map((mode, i) => {
          const f = MODE_FACTS[mode];
          const active = i === highlighted;
          return (
            <button
              key={mode}
              type="button"
              role="radio"
              aria-checked={current === mode}
              aria-label={t(MODE_LABEL_KEY[mode])}
              disabled={disabled}
              onClick={() => onPick(mode)}
              data-testid={`compact-mode-${mode}`}
              data-active={active ? "true" : "false"}
              className="flex flex-col items-center justify-center gap-1 px-1 py-1.5 min-h-11 sm:min-h-0 cursor-pointer disabled:cursor-not-allowed focus-visible:outline-none focus-visible:ring-1"
              style={{ ["--tw-ring-color" as string]: C.borderAccent }}
            >
              {/* Ab sm eine Zeile — die Slot-Kachel ist voll, jede zweite
                  Zeile kostet dort echten Platz. Auf dem Handy passen bei vier
                  Spalten „boost" und „≈60 W" nicht nebeneinander (sie wurden
                  abgeschnitten), also untereinander. Das „≈" trennt die
                  Referenzmessung sichtbar von den Live-Werten in der
                  Telemetrie-Spalte daneben (HONESTY RULE). */}
              <span
                className="flex flex-col sm:flex-row items-center sm:gap-1 leading-none text-[11px]"
                style={{
                  fontWeight: active ? 600 : 500,
                  color: active ? C.textPrimary : C.textMuted,
                }}
              >
                <span className="flex items-center gap-1 whitespace-nowrap">
                  {f.risky && (
                    <AlertTriangle
                      size={9}
                      aria-hidden
                      style={{ color: active ? STATUS_TEXT.warning : C.textDim }}
                    />
                  )}
                  {t(MODE_LABEL_KEY[mode])}
                </span>
                <span
                  className="tabular-nums whitespace-nowrap text-[9px] font-normal"
                  style={{ color: active ? C.textMuted : C.textDim }}
                >
                  ≈{num(locale, f.watt, 0)} {t("unitWatt")}
                </span>
              </span>
              {/* Stromtreppe: Länge trägt den Verbrauch. Statisch gesetzt und
                  über transform skaliert — kein Layout, kein Ruckeln. */}
              <span
                aria-hidden
                data-testid={`compact-bar-${mode}`}
                className="w-full rounded-full"
                style={{
                  height: "4px",
                  background: active ? C.accentDeep : C.textDim,
                  opacity: active ? 1 : 0.8,
                  transform: `scaleX(${f.watt / ROW_MAX.watt})`,
                  transformOrigin: "left",
                }}
              />
            </button>
          );
        })}
      </div>
    </div>
  );
}

// ── Der Streifen in der Slot-Kachel ─────────────────────────────────────────

export function DeviceModeStrip({
  device,
  canControl,
}: {
  device: Device;
  canControl: boolean;
}) {
  const t = useTranslations("runtimes.devices");
  const locale = useLocale();
  const queryClient = useQueryClient();
  const reduce = useReducedMotion();

  const [open, setOpen] = useState(false);

  const state = device.device_state;
  const currentMode: GpuMode | null =
    state && state.gpu_mode !== "unknown" ? (state.gpu_mode as GpuMode) : null;

  // Optimistisches Ziel: der Klick soll sofort sichtbar sein, obwohl das Gerät
  // erst beim nächsten Heartbeat reagiert. Sobald der Server bestätigt hat,
  // führt wieder `device.desired_state` — sonst würde ein fremder Wechsel
  // (anderer Nutzer, zweiter Tab) hier still überschrieben.
  const [optimistic, setOptimistic] = useState<GpuMode | null>(null);
  const [pickedAt, setPickedAt] = useState<number | null>(null);
  const [elapsed, setElapsed] = useState(0);

  const targetMode: GpuMode | null = optimistic ?? device.desired_state?.gpu_mode ?? null;

  // Gesperrt = ein Klick könnte nichts bewirken. Dann gibt es auch kein
  // „wird übernommen": das Warten hätte kein Ende, und der Sperr-Hinweis
  // sagt stattdessen, was zuerst passieren muss.
  const lock = switchLockFor(device);
  const pending = !lock && !!targetMode && targetMode !== currentMode;

  useEffect(() => {
    if (optimistic && currentMode === optimistic) {
      setOptimistic(null);
      setPickedAt(null);
    }
  }, [optimistic, currentMode]);

  useEffect(() => {
    if (!pending || pickedAt === null) return;
    const id = setInterval(() => setElapsed((Date.now() - pickedAt) / 1000), 250);
    return () => clearInterval(id);
  }, [pending, pickedAt]);

  const mutation = useMutation({
    // PUT ersetzt den Soll-Zustand vollständig — die übrigen Vorgaben
    // (oom_guard, mtu, …) müssen mit, sonst löscht ein Moduswechsel sie.
    mutationFn: (mode: GpuMode) => {
      const next: DesiredState = { ...(device.desired_state ?? {}), gpu_mode: mode };
      return api.nodes.setDesiredState(device.host_id, next);
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["devices"] }),
    onError: () => {
      // Fehlgeschlagen heisst: es gibt kein Ziel. Die gestrichelte Umrandung
      // muss weg, sonst wartet der Operator auf etwas, das nie kommt.
      setOptimistic(null);
      setPickedAt(null);
    },
  });

  const pick = (mode: GpuMode) => {
    if (!canControl || lock || mode === targetMode) return;
    setOptimistic(mode);
    setPickedAt(Date.now());
    setElapsed(0);
    mutation.mutate(mode);
  };

  const remaining = Math.max(0, Math.ceil(HEARTBEAT_SECONDS - elapsed));
  const progressPct = Math.min(100, (elapsed / HEARTBEAT_SECONDS) * 100);
  const shownMode: GpuMode | null = targetMode ?? currentMode;

  return (
    <div
      data-testid="device-control"
      data-slug={device.slug}
      className="flex flex-col gap-2 px-4 py-2.5"
      style={{ borderTop: `1px solid ${C.borderSubtle}`, background: C.bgBase }}
    >
      <div className="flex flex-col sm:flex-row sm:items-center gap-1.5 sm:gap-2.5">
        {/* Sagt, was der Streifen ist — die Kachel trägt schon zwei andere
            Schalter (Rezept, Start/Stop), er darf nicht mit ihnen verschwimmen.
            Auch auf dem Handy sichtbar, dort über dem Schalter: vier Modus-
            namen ohne Überschrift erklären sich nicht von selbst. */}
        <span
          className="shrink-0 text-[10px] font-medium uppercase"
          style={{ color: C.textDim, letterSpacing: "0.08em" }}
        >
          {t("stripLabel")}
        </span>
        {/* Gesperrt: sichtbar gedimmt, und ohne Ziel-Markierung — ein Reiter
            auf dem Soll würde eine Einstellung behaupten, die die Box nie
            bestätigt hat. */}
        <div className="flex-1 min-w-0" style={lock ? { opacity: 0.55 } : undefined}>
          <CompactModeSwitch
            current={currentMode}
            target={lock ? null : targetMode}
            pending={pending}
            disabled={!canControl || !!lock || mutation.isPending}
            onPick={pick}
          />
        </div>
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          data-testid="device-toggle-detail"
          className="shrink-0 flex items-center gap-1 rounded-md px-2 min-h-11 sm:min-h-7 cursor-pointer text-[11px]"
          style={{ color: C.textMuted, border: `1px solid ${C.border}` }}
        >
          {t("showNumbers")}
          <ChevronDown
            size={12}
            aria-hidden
            className="transition-transform duration-150 motion-reduce:transition-none"
            style={{ transform: open ? "rotate(180deg)" : "none" }}
          />
        </button>
      </div>

      {/* Die Kernaussage — immer sichtbar, auch eingeklappt. Ohne sie versteht
          niemand, warum man freiwillig die sparsamste Stufe wählt. Mit
          „Gemessen:" davor, damit sie nicht als Live-Wert dieser Box gelesen
          wird (HONESTY RULE). */}
      <p className="text-[11px]" style={{ color: C.textSecondary, lineHeight: 1.45 }}>
        {shownMode
          ? t("sameSpeedShort", { tokens: `${num(locale, MODE_FACTS[shownMode].tokensPerSec)} ${t("unitTokens")}` })
          : t("sameSpeed")}
      </p>

      {/* Gesperrt — ein Satz, was fehlt. Warn-Ocker: die Box ist nicht kaputt,
          MC kann sie nur gerade nicht stellen. Der Schalter bleibt sichtbar,
          damit klar ist, dass es ihn gibt. */}
      {lock && (
        <div
          data-testid="device-lock-hint"
          data-lock={lock}
          className="flex items-start gap-2 rounded-lg px-2.5 py-1.5"
          style={{ background: `${C.warning}0F`, border: `1px solid ${C.warning}33` }}
        >
          <Lock size={11} aria-hidden style={{ color: STATUS_TEXT.warning, marginTop: 2 }} />
          <span className="text-[11px]" style={{ color: STATUS_TEXT.warning, lineHeight: 1.45 }}>
            {lock === "no_device_state" && t("lockNoDeviceState")}
            {lock === "stale" &&
              (device.age_s != null
                ? t("lockStale", { age: ageLabel(locale, device.age_s) })
                : t("lockStaleNever"))}
            {lock === "unknown_mode" && (
              <>
                {t("lockUnknownMode")}{" "}
                <code className="font-mono text-[10px]">--install --allow-control</code>{" "}
                {t("lockUnknownModeTail")}
              </>
            )}
          </span>
        </div>
      )}

      {/* Nachziehen — Info-Blau, mit sichtbarem Fortschritt statt Spinner. */}
      {pending && (
        <div
          data-testid="device-pending"
          className="rounded-lg px-2.5 py-1.5"
          style={{ background: `${C.info}0F`, border: `1px solid ${C.info}33` }}
        >
          <div className="flex items-center gap-2 text-[11px]" style={{ color: C.info }}>
            {mutation.isPending && <Loader2 size={11} className="animate-spin" aria-hidden />}
            <span>
              {t("pendingTo", { mode: t(MODE_LABEL_KEY[targetMode as GpuMode]) })}
              {pickedAt !== null && remaining > 0 ? ` · ${t("pendingSeconds", { seconds: remaining })}` : ""}
            </span>
          </div>
          {/* Fortschritt nur, wenn wir wissen, wann umgestellt wurde. Kam der
              Soll von woanders, wäre jede Füllung geraten — ein voller Balken
              sähe aus wie "fertig". */}
          {pickedAt !== null && (
            <div className="mt-1 rounded-full overflow-hidden" style={{ height: "2px", background: `${C.info}26` }}>
              {/* scaleX statt width: 15 Sekunden lang jedes Bild neu layouten
                  würde man sehen. */}
              <div
                style={{
                  height: "100%",
                  background: C.info,
                  width: "100%",
                  transform: `scaleX(${progressPct / 100})`,
                  transformOrigin: "left",
                  transition: reduce ? "none" : "transform 0.25s linear",
                }}
              />
            </div>
          )}
          <p className="mt-1 text-[10px]" style={{ color: C.textDim }}>
            {t("pendingHint")}
          </p>
        </div>
      )}

      {/* boost ist die einzige Stufe, die die Box gefährdet — genau einmal
          sagen, ruhig, und nur wenn sie gewählt ist. */}
      {shownMode === "boost" && (
        <div
          data-testid="device-boost-warning"
          className="flex items-start gap-2 rounded-lg px-2.5 py-1.5"
          style={{ background: `${C.warning}0F`, border: `1px solid ${C.warning}33` }}
        >
          <AlertTriangle size={11} aria-hidden style={{ color: STATUS_TEXT.warning, marginTop: 2 }} />
          <span className="text-[11px]" style={{ color: STATUS_TEXT.warning, lineHeight: 1.45 }}>
            {t("boostRiskHint")}
          </span>
        </div>
      )}

      {device.last_error && (
        <div
          data-testid="device-last-error"
          className="rounded-lg px-2.5 py-1.5 text-[11px]"
          style={{ background: `${C.error}0F`, border: `1px solid ${C.error}26`, color: STATUS_TEXT.error }}
        >
          {t("deviceError", { error: device.last_error })}
        </div>
      )}

      {mutation.isError && (
        <div
          data-testid="device-apply-failed"
          className="rounded-lg px-2.5 py-1.5 text-[11px]"
          style={{ background: `${C.error}0F`, border: `1px solid ${C.error}26`, color: STATUS_TEXT.error }}
        >
          {t("applyFailed")}
        </div>
      )}

      {!state && !lock && (
        <p data-testid="device-no-report" className="text-[11px]" style={{ color: C.textMuted }}>
          {t("notReporting")}
        </p>
      )}

      {!canControl && (
        <p className="text-[10px]" style={{ color: C.textDim }}>{t("readOnlyHint")}</p>
      )}

      {/* Volle Ansicht: das Diagramm, das die Behauptung „gleich schnell"
          belegt, statt sie nur zu behaupten. */}
      {open && (
        <div
          data-testid="device-detail"
          className="rounded-lg px-3 py-3 mt-0.5"
          style={{ background: C.bgSurface, border: `1px solid ${C.border}` }}
        >
          <ModeScale
            current={currentMode}
            target={lock ? null : targetMode}
            pending={pending}
            disabled={!canControl || !!lock || mutation.isPending}
            onPick={pick}
          />
          <p className="mt-3 text-[11px]" style={{ color: C.textSecondary, lineHeight: 1.45 }}>
            {t("sameSpeed")}
          </p>
          {shownMode && <FactsReadout mode={shownMode} />}
          <p className="mt-2 text-[10px]" style={{ color: C.textDim }}>
            {t("measuredNote")}
          </p>
        </div>
      )}
    </div>
  );
}

// ── Geräte-Daten für die Host-Liste ──────────────────────────────────────────

/**
 * Alle Geräte auf einmal, als Karte host_id → Gerät.
 *
 * Eine Query für die ganze Liste statt eine je Zeile: das Backend liefert
 * `/nodes/devices` genau dafür. Enthalten sind nur je gepaarte Boxen — ein
 * Host ohne node-agent taucht nicht auf und bekommt darum auch keinen
 * Schalter (und keine leere Fläche, wo einer sein könnte).
 */
export function useDevices(): Map<string, Device> {
  const { data } = useQuery<Device[]>({
    queryKey: ["devices"],
    queryFn: api.nodes.devices,
    // Der Agent meldet sich alle 15 s — häufiger nachfragen bringt nichts,
    // seltener liesse „wird übernommen" zu lange stehen.
    refetchInterval: 10_000,
  });
  const map = new Map<string, Device>();
  for (const d of data ?? []) map.set(d.host_id, d);
  return map;
}

export { REASON_LABEL_KEY };
