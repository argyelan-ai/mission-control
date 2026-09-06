"use client";

/**
 * ModeList — Cockpit-Gruppe „Mode" (Spec §4.2). Radio-Liste, 4 Zeilen à 48px,
 * Kopfzeile Mode · Power · Fan, aktive Zeile accent-subtle, KEINE
 * Speed-Spalte und KEIN „Auto" (bewusst gestrichen — Spec §4: "Keine
 * Speed-Spalte, kein Auto"). Fusszeile "this box · now {temp}".
 *
 * Teilt Klick-/Sperr-/Nachzieh-Logik mit dem Vierer auf der Karte über
 * `useDeviceModeControl` (Spec §4: "Karte (Vierer) und Cockpit zeigen
 * denselben Zustand") — kein zweiter Mutations-Pfad.
 *
 * Fan-Spalte: ein qualitatives Wort je Stufe (silent/quiet/audible/loud), aus
 * derselben Referenzmessung wie die Watt-Spalte (MODE_FACTS) — KEINE
 * Live-Fan-Prozentzahl in der Fusszeile, weil `DeviceState` keine trägt
 * (HONESTY RULE, wie Stage.tsx's fehlende Laufzeit).
 */

import { useTranslations } from "next-intl";
import { Loader2, Lock } from "lucide-react";
import { GPU_MODES, type Device, type GpuMode } from "@/lib/types";
import { C, STATUS_TEXT } from "@/lib/colors";
import { MODE_FACTS, useDeviceModeControl } from "../../DeviceControl";

const MODE_LABEL_KEY: Record<GpuMode, string> = {
  "eco+": "modeEcoPlus",
  eco: "modeEco",
  normal: "modeNormal",
  boost: "modeBoost",
};

const FAN_WORD_KEY: Record<GpuMode, string> = {
  "eco+": "fanSilent",
  eco: "fanQuiet",
  normal: "fanAudible",
  boost: "fanLoud",
};

export function ModeList({
  device,
  canControl,
}: {
  device?: Device;
  canControl: boolean;
}) {
  const t = useTranslations("runtimes.cockpit");
  const tDevices = useTranslations("runtimes.devices");

  if (!device) {
    return (
      <p className="text-[11px]" data-testid="mode-list-no-device" style={{ color: C.textMuted }}>
        {t("modeNoDevice")}
      </p>
    );
  }

  return (
    <ModeListBody device={device} canControl={canControl} t={t} tDevices={tDevices} />
  );
}

function ModeListBody({
  device,
  canControl,
  t,
  tDevices,
}: {
  device: Device;
  canControl: boolean;
  t: ReturnType<typeof useTranslations>;
  tDevices: ReturnType<typeof useTranslations>;
}) {
  const { currentMode, targetMode, lock, pending, mutation, pick, shownMode } = useDeviceModeControl(
    device,
    canControl
  );
  const disabled = !canControl || !!lock || mutation.isPending;
  const selected = currentMode ?? targetMode;
  const tempC = device.device_state?.gpu_temp_c ?? null;

  return (
    <div data-testid="mode-list" style={lock ? { opacity: 0.5 } : undefined}>
      <div
        className="grid rounded-[3px] overflow-hidden"
        style={{ border: `1px solid ${C.border}` }}
        role="radiogroup"
        aria-label={tDevices("modeGroupLabel")}
      >
        <div
          className="grid items-center font-mono uppercase"
          style={{
            gridTemplateColumns: "18px 1fr 56px 72px",
            gap: "10px",
            padding: "0 12px",
            minHeight: "28px",
            background: C.bgDeep,
            fontSize: "9px",
            letterSpacing: "0.12em",
            color: C.textMuted,
          }}
        >
          <span aria-hidden />
          <span>{t("colMode")}</span>
          <span style={{ textAlign: "right" }}>{t("colPower")}</span>
          <span style={{ textAlign: "right" }}>{t("colFan")}</span>
        </div>
        {GPU_MODES.map((mode, i) => {
          const active = mode === selected;
          const facts = MODE_FACTS[mode];
          return (
            <button
              key={mode}
              type="button"
              role="radio"
              aria-checked={currentMode === mode}
              disabled={disabled}
              onClick={() => pick(mode)}
              data-testid={`cockpit-mode-${mode}`}
              className="grid items-center text-left cursor-pointer disabled:cursor-not-allowed"
              style={{
                gridTemplateColumns: "18px 1fr 56px 72px",
                gap: "10px",
                padding: "0 12px",
                minHeight: "48px",
                fontSize: "13px",
                color: active ? C.textPrimary : C.textSecondary,
                background: active ? C.accentSubtle : "transparent",
                borderTop: i > 0 ? `1px solid ${C.border}` : "none",
              }}
            >
              <span
                aria-hidden
                className="rounded-full inline-block"
                style={{
                  width: "12px",
                  height: "12px",
                  border: `1.5px solid ${active ? C.accent : C.textMuted}`,
                  background: active
                    ? `radial-gradient(circle, ${C.accent} 40%, transparent 44%)`
                    : "transparent",
                }}
              />
              <span style={{ fontWeight: 500 }}>
                {tDevices(MODE_LABEL_KEY[mode])}
              </span>
              <span
                className="font-mono tabular-nums"
                style={{ fontSize: "11px", textAlign: "right", color: active ? C.textSecondary : C.textMuted }}
              >
                {facts.watt.toFixed(1)} {tDevices("unitWatt")}
              </span>
              <span
                className="font-mono"
                style={{ fontSize: "11px", textAlign: "right", color: active ? C.textSecondary : C.textMuted }}
              >
                {t(FAN_WORD_KEY[mode])}
              </span>
            </button>
          );
        })}
      </div>

      {lock && (
        <div className="flex items-center gap-1.5 mt-2 text-[11px]" style={{ color: STATUS_TEXT.warning }} data-testid="mode-list-lock">
          <Lock size={11} aria-hidden />
          <span>
            {lock === "no_device_state" && tDevices("lockNoDeviceState")}
            {lock === "stale" && tDevices("lockStaleNever")}
            {lock === "unknown_mode" && tDevices("lockUnknownMode")}
          </span>
        </div>
      )}

      {!lock && pending && (
        <div className="flex items-center gap-1.5 mt-2 text-[11px]" style={{ color: C.info }} data-testid="mode-list-pending">
          {mutation.isPending && <Loader2 size={11} className="animate-spin" aria-hidden />}
          <span>{tDevices("pendingTo", { mode: tDevices(MODE_LABEL_KEY[targetMode as GpuMode]) })}</span>
        </div>
      )}

      <p className="mt-2 font-mono text-[11px]" style={{ color: C.textMuted }} data-testid="mode-list-footer">
        {tempC != null ? t("footerNow", { temp: `${tempC} ${tDevices("unitTemp")}` }) : t("footerNowUnknown")}
      </p>
      {shownMode == null && !lock && (
        <span className="sr-only" data-testid="mode-list-no-selection" />
      )}
    </div>
  );
}
