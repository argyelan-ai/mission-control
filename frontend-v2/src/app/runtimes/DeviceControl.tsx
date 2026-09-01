"use client";

/**
 * Geräte-Steuerung — GPU-Modus je Box
 * (docs/plans/2026-09-01-geraete-steuerung-vertrag.md, Gewerk C).
 *
 * WARUM diese Form, und nicht ein Dropdown:
 * Die eigentliche Botschaft der Messung ist kontraintuitiv — die Box wird
 * NICHT langsamer, wenn man ihr Strom wegnimmt. Ein Dropdown würde genau das
 * verschweigen und der Operator bliebe aus Angst auf `boost`, dem einzigen
 * Modus, in dem die Box unter Dauerlast hart abschaltet.
 *
 * Darum ist der Schalter hier zugleich das Diagramm: vier Spalten (kühl →
 * heiss), darin drei Balkenreihen mit den gemessenen Werten. Alle Balken
 * wachsen von Null aus derselben Reihen-Skala — nichts ist verzerrt. Die
 * Reihe „Erzeugung" steht dadurch schnurgerade, „Strom" fällt als Treppe.
 * Man sieht die Aussage, bevor man sie liest.
 *
 * Soll ≠ Ist ist sichtbarer Teil der Bedienung: MC schickt keinen Befehl,
 * sondern legt einen Soll-Zustand ab, den das Gerät beim nächsten Heartbeat
 * (bis zu 15 s) abholt. Der gefüllte Reiter zeigt den Ist-Zustand, die
 * gestrichelte Umrandung das Ziel. Das ist kein Fehler, sondern Wartezeit —
 * deshalb Info-Blau, nie Rot.
 */

import { useEffect, useMemo, useState } from "react";
import { motion, useReducedMotion } from "framer-motion";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Cpu, Loader2 } from "lucide-react";
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
import { useAppStore } from "@/lib/store";
import { C, STATUS_TEXT } from "@/lib/colors";
import { Section, SectionOrFragment } from "@/components/shared/Section";

// ── Gemessene Werte ───────────────────────────────────────────────────────────
// Marks Messung 16.08.2026, Qwen38-27B auf einer Box (Vertrag, Abschnitt
// „GPU-Modi"). Fest verdrahtet und nicht aus der Telemetrie gerechnet: es ist
// eine Referenzmessung, die erklären soll, was die Wahl kostet — keine
// Live-Zahl. Live-Werte stehen daneben in der Kopfzeile der Kachel.
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
const STATUS_COLOR: Record<DeviceStatus, string> = {
  green: C.online,
  yellow: C.warning,
  red: C.error,
  grey: C.textDim,
};

const STATUS_TONE: Record<DeviceStatus, string> = {
  green: STATUS_TEXT.online,
  yellow: STATUS_TEXT.warning,
  red: STATUS_TEXT.error,
  grey: C.textDim,
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

// Feste Zeilenhöhen: die Beschriftungsspalte links muss auf den Pixel mit den
// Balkenreihen rechts fluchten, sonst zerfällt das Diagramm in zwei Listen.
const H = { head: 30, row: 80, foot: 26 } as const;

// Höhe der Bezugslinie über der Erzeugungs-Reihe — aus den Messwerten
// gerechnet, damit sie nicht lügen kann. `- 6` = paddingBottom der Reihe.
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

function HealthDot({ status, size = 8 }: { status: DeviceStatus; size?: number }) {
  const color = STATUS_COLOR[status];
  return (
    <span
      data-testid={`health-dot-${status}`}
      aria-hidden
      style={{
        width: size,
        height: size,
        borderRadius: "50%",
        background: color,
        // Der Ring hält den Punkt auch auf dunklem Grund lesbar, ohne Glow.
        boxShadow: `0 0 0 3px ${color}1F`,
        display: "inline-block",
        flexShrink: 0,
      }}
    />
  );
}

/** Live-Wert aus der Telemetrie. Fehlt der Wert, steht ein Strich — nie eine 0. */
function LiveValue({ label, value }: { label: string; value: string }) {
  return (
    <span className="flex items-baseline gap-1" style={{ fontSize: "11px" }}>
      <span style={{ color: C.textDim }}>{label}</span>
      <span className="font-semibold tabular-nums" style={{ color: C.textSecondary }}>
        {value}
      </span>
    </span>
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
      <span style={{ fontSize: "11px", color: C.textMuted }}>{name}</span>
      <span style={{ fontSize: "9px", color: C.textDim }}>{unit}</span>
    </div>
  );
}

// ── Modus-Skala: Schalter und Diagramm in einem Objekt ───────────────────────

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
  /** Reduzierte Bewegung: Höhe ohne Übergang setzen. */
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
  // legt er sich unter das Ziel, damit die Auswahl trotzdem sichtbar ist.
  // Ist BEIDES offen (frische, noch nicht gekoppelte Box), wird gar nichts
  // hervorgehoben — ein Reiter auf "eco" würde eine Einstellung behaupten,
  // die niemand gemacht hat.
  const selected = current ?? target;
  const solidIndex = selected ? GPU_MODES.indexOf(selected) : -1;
  const targetIndex = target ? GPU_MODES.indexOf(target) : -1;
  const showTarget = pending && targetIndex >= 0 && targetIndex !== solidIndex;

  const highlighted = showTarget ? targetIndex : solidIndex;

  return (
    <div>
      {/* Skalen-Beschriftung: kühl/sparsam links, heiss/schnell rechts. */}
      <div
        className="flex items-center justify-between mb-2"
        style={{ fontSize: "10px", letterSpacing: "0.06em", textTransform: "uppercase", color: C.textDim }}
      >
        <span>{t("scaleLow")}</span>
        <span
          aria-hidden
          className="flex-1 mx-3"
          style={{
            height: "1px",
            background: `linear-gradient(90deg, ${C.border}, ${C.borderActive})`,
          }}
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
            className="flex items-center"
            style={{ height: H.foot, fontSize: "10px", color: C.textDim }}
          >
            {t("rowPrefill")}
          </div>
        </div>

        {/* Spaltenfeld mit gleitendem Reiter darunter. */}
        <div className="relative flex-1" role="radiogroup" aria-label={t("modeGroupLabel")}>
          {/* Gefüllter Reiter = Ist. Gleitet, springt nicht. */}
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
              style={{
                width: "25%",
                left: 0,
                border: `1px dashed ${C.info}`,
                background: `${C.info}0F`,
              }}
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
            style={{
              left: 0,
              right: 0,
              top: GEN_REFERENCE_TOP,
              borderTop: `1px dashed ${C.borderAccent}`,
            }}
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
                  {/* Kopf: Name + Risiko-Hinweis für boost. */}
                  <span
                    className="flex items-center justify-center gap-1"
                    style={{
                      height: H.head,
                      fontSize: "12px",
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
                    className="flex items-center justify-center tabular-nums"
                    style={{
                      height: H.foot,
                      fontSize: "10px",
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

// ── Zahlen zum gewählten Modus ───────────────────────────────────────────────

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
          <span style={{ fontSize: "10px", color: C.textDim }}>{t(it.k)}</span>
          <span className="tabular-nums" style={{ fontSize: "15px", color: C.textPrimary, fontWeight: 600 }}>
            {it.v}
          </span>
        </div>
      ))}
    </div>
  );
}

// ── Gerätekachel ─────────────────────────────────────────────────────────────

/** Exportiert, damit die Zustands-Vorschau (scripts/device-preview.tsx) genau
 *  diese Komponente rendert statt eine Nachbildung davon. */
export function DeviceCard({ device, canControl }: { device: Device; canControl: boolean }) {
  const t = useTranslations("runtimes.devices");
  const locale = useLocale();
  const queryClient = useQueryClient();
  const reduce = useReducedMotion();

  const state = device.device_state;
  const currentMode: GpuMode | null =
    state && state.gpu_mode !== "unknown" ? (state.gpu_mode as GpuMode) : null;

  // Optimistisches Ziel: der Klick soll sofort sichtbar sein, obwohl das Gerät
  // erst beim nächsten Heartbeat reagiert. Sobald der Server bestätigt hat,
  // führt wieder `device.desired` — sonst würde ein fremder Wechsel (anderer
  // Nutzer, zweiter Tab) hier still überschrieben.
  const [optimistic, setOptimistic] = useState<GpuMode | null>(null);
  const [pickedAt, setPickedAt] = useState<number | null>(null);
  const [elapsed, setElapsed] = useState(0);

  const targetMode: GpuMode | null = optimistic ?? device.desired_state?.gpu_mode ?? null;
  const pending = !!targetMode && targetMode !== currentMode;

  useEffect(() => {
    // Ziel erreicht → Optimismus fallen lassen, Countdown zurücksetzen.
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
    if (!canControl || mode === targetMode) return;
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
      data-testid="device-card"
      data-slug={device.slug}
      className="rounded-xl overflow-hidden"
      style={{ background: C.bgSurface, border: `1px solid ${C.border}` }}
    >
      {/* Kopfzeile: Name, Ampel, Live-Werte */}
      <div
        className="flex flex-wrap items-center gap-x-4 gap-y-2 px-4 py-3 border-b"
        style={{ borderColor: C.border, background: C.bgBase }}
      >
        <div className="flex items-center gap-2 min-w-0">
          <Cpu size={13} aria-hidden style={{ color: C.textDim }} />
          <span className="text-sm font-semibold truncate" style={{ color: C.textPrimary }}>
            {device.display_name}
          </span>
        </div>

        <div className="flex items-center gap-1.5">
          <HealthDot status={device.status} />
          <span style={{ fontSize: "11px", color: STATUS_TONE[device.status] }}>
            {t(REASON_LABEL_KEY[device.reason])}
          </span>
        </div>

        <div className="flex items-center gap-3 ml-auto">
          <LiveValue
            label={t("liveClock")}
            value={state?.gpu_clock_mhz != null ? `${state.gpu_clock_mhz} MHz` : "—"}
          />
          <LiveValue
            label={t("livePower")}
            value={state?.gpu_power_w != null ? `${num(locale, state.gpu_power_w)} ${t("unitWatt")}` : "—"}
          />
          <LiveValue
            label={t("liveHeat")}
            value={state?.gpu_temp_c != null ? `${state.gpu_temp_c} ${t("unitTemp")}` : "—"}
          />
        </div>
      </div>

      <div className="px-4 py-4">
        <ModeScale
          current={currentMode}
          target={targetMode}
          pending={pending}
          disabled={!canControl || mutation.isPending}
          onPick={pick}
        />

        {/* Die Kernaussage — direkt unter der Reihe, die schnurgerade steht. */}
        <p className="mt-3" style={{ fontSize: "12px", color: C.textSecondary, lineHeight: 1.5 }}>
          {t("sameSpeed")}
        </p>

        {shownMode && <FactsReadout mode={shownMode} />}

        <p className="mt-2" style={{ fontSize: "10px", color: C.textDim }}>
          {t("measuredNote")}
        </p>

        {/* Nachziehen — Info-Blau, mit sichtbarem Fortschritt statt Spinner. */}
        {pending && (
          <div
            data-testid="device-pending"
            className="mt-3 rounded-lg px-3 py-2"
            style={{ background: `${C.info}0F`, border: `1px solid ${C.info}33` }}
          >
            <div className="flex items-center gap-2" style={{ fontSize: "11px", color: C.info }}>
              {mutation.isPending && <Loader2 size={11} className="animate-spin" aria-hidden />}
              <span>
                {t("pendingTo", { mode: t(MODE_LABEL_KEY[targetMode as GpuMode]) })}
                {pickedAt !== null && remaining > 0 ? ` · ${t("pendingSeconds", { seconds: remaining })}` : ""}
              </span>
            </div>
            {/* Fortschritt nur, wenn wir wissen, wann umgestellt wurde. Kam
                der Soll von woanders (anderer Nutzer, Neuladen), wäre jede
                Füllung geraten — ein voller Balken sähe aus wie "fertig". */}
            {pickedAt !== null && (
              <div className="mt-1.5 rounded-full overflow-hidden" style={{ height: "2px", background: `${C.info}26` }}>
                {/* scaleX statt width: 15 Sekunden lang jedes Bild neu
                    layouten würde man sehen. */}
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
            <p className="mt-1.5" style={{ fontSize: "10px", color: C.textDim }}>
              {t("pendingHint")}
            </p>
          </div>
        )}

        {/* boost ist die einzige Stufe, die die Box gefährdet — genau einmal
            sagen, ruhig, und nur wenn sie gewählt ist. */}
        {shownMode === "boost" && (
          <div
            data-testid="device-boost-warning"
            className="mt-3 flex items-start gap-2 rounded-lg px-3 py-2"
            style={{ background: `${C.warning}0F`, border: `1px solid ${C.warning}33` }}
          >
            <AlertTriangle size={12} aria-hidden style={{ color: STATUS_TEXT.warning, marginTop: 1 }} />
            <span style={{ fontSize: "11px", color: STATUS_TEXT.warning, lineHeight: 1.5 }}>
              {t("boostRiskHint")}
            </span>
          </div>
        )}

        {device.last_error && (
          <div
            data-testid="device-last-error"
            className="mt-3 rounded-lg px-3 py-2"
            style={{ background: `${C.error}0F`, border: `1px solid ${C.error}26`, fontSize: "11px", color: STATUS_TEXT.error }}
          >
            {t("deviceError", { error: device.last_error })}
          </div>
        )}

        {mutation.isError && (
          <div
            data-testid="device-apply-failed"
            className="mt-3 rounded-lg px-3 py-2"
            style={{ background: `${C.error}0F`, border: `1px solid ${C.error}26`, fontSize: "11px", color: STATUS_TEXT.error }}
          >
            {t("applyFailed")}
          </div>
        )}

        {!state && (
          <p data-testid="device-no-report" className="mt-3" style={{ fontSize: "11px", color: C.textMuted }}>
            {t("notReporting")}
          </p>
        )}

        {!canControl && (
          <p className="mt-3" style={{ fontSize: "10px", color: C.textDim }}>
            {t("readOnlyHint")}
          </p>
        )}
      </div>
    </div>
  );
}

// ── Abschnitt ────────────────────────────────────────────────────────────────

export function DeviceControl({ embedded = false }: { embedded?: boolean } = {}) {
  const t = useTranslations("runtimes.devices");
  const currentUser = useAppStore((s) => s.currentUser);
  const canControl = currentUser?.role === "admin";

  const { data, isLoading, isError } = useQuery<Device[]>({
    queryKey: ["devices"],
    queryFn: api.nodes.devices,
    // Der Agent meldet sich alle 15 s — häufiger nachfragen bringt nichts,
    // seltener liesse „wird übernommen" zu lange stehen.
    refetchInterval: 10_000,
  });

  const devices = useMemo(() => data ?? [], [data]);

  return (
    <SectionOrFragment
      embedded={embedded}
      id="devices"
      title={t("title")}
      hint={t("subtitle")}
      count={devices.length}
    >
      {isLoading && (
        <div className="flex items-center gap-2 py-2" style={{ color: C.textMuted }}>
          <Loader2 size={13} className="animate-spin" />
          <span className="text-xs">{t("loading")}</span>
        </div>
      )}

      {isError && (
        <div className="text-xs py-2" style={{ color: STATUS_TEXT.error }}>
          {t("loadError")}
        </div>
      )}

      {!isLoading && !isError && devices.length === 0 && (
        <div className="flex items-center gap-2 text-xs py-6 justify-center" style={{ color: C.textMuted }}>
          <Cpu size={13} />
          {t("empty")}
        </div>
      )}

      <div className="flex flex-col gap-3">
        {devices.map((d) => (
          <DeviceCard key={d.host_id} device={d} canControl={canControl} />
        ))}
      </div>
    </SectionOrFragment>
  );
}

// Section wird re-exportiert, damit die Seite den Abschnitt auch eingebettet
// (ohne eigene Kopfzeile) einhängen kann — gleiche Signatur wie HostsSection.
export { Section };
