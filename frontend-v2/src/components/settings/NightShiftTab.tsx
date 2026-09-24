"use client";

/**
 * Settings → Night shift (ROADMAP E2): on/off, the time window, its time
 * zone and the cloud share. Saved in app_settings; mc-worker reads them on
 * every tick, so a change counts from the next minute on.
 */

import { useEffect, useMemo, useState } from "react";
import { motion } from "framer-motion";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useLocale, useTranslations } from "next-intl";
import { Loader2, Save } from "lucide-react";
import { api } from "@/lib/api";
import { notify } from "@/lib/notify";
import { C, STATUS_TEXT } from "@/lib/colors";
import { isHeadsDisabled } from "@/lib/heads";
import {
  browserTimeZone,
  invalidConfigFields,
  isHHMM,
  nightErrorKey,
  type NightConfig,
  type NightConfigUpdate,
} from "@/lib/nightShift";
import { NightSwitch } from "@/components/night/NightSwitch";

const FALLBACK_ZONES = [
  "UTC",
  "Europe/London",
  "Europe/Berlin",
  "Europe/Paris",
  "America/New_York",
  "America/Chicago",
  "America/Los_Angeles",
  "Asia/Tokyo",
  "Australia/Sydney",
];

function allZones(): string[] {
  try {
    const fn = (Intl as unknown as { supportedValuesOf?: (k: string) => string[] }).supportedValuesOf;
    const list = fn ? fn("timeZone") : [];
    return list.length > 0 ? (list.includes("UTC") ? list : ["UTC", ...list]) : FALLBACK_ZONES;
  } catch {
    return FALLBACK_ZONES;
  }
}

const inputCls = "w-full rounded-lg px-3 min-h-[44px] text-base sm:text-sm outline-none";
const inputStyle = {
  backgroundColor: C.bgDeep,
  border: "1px solid var(--color-border)",
  color: "var(--color-text-primary)",
} as const;

export const NIGHT_CONFIG_KEY = ["nightShift", "config"] as const;

export function NightShiftTab() {
  const t = useTranslations("nightShift.settings");
  const tn = useTranslations("nightShift");
  const locale = useLocale();
  const qc = useQueryClient();
  const q = useQuery<NightConfig>({
    queryKey: NIGHT_CONFIG_KEY,
    queryFn: () => api.nightShift.config(),
    retry: false,
  });
  const [form, setForm] = useState<NightConfigUpdate | null>(null);
  const [badFields, setBadFields] = useState<string[]>([]);
  useEffect(() => {
    if (q.data && form == null) {
      const { enabled, start, end, timezone, cloud_share } = q.data;
      setForm({ enabled, start, end, timezone, cloud_share });
    }
  }, [q.data, form]);
  const zones = useMemo(() => allZones(), []);
  const deviceZone = browserTimeZone();

  const save = useMutation({
    mutationFn: (body: NightConfigUpdate) => api.nightShift.saveConfig(body),
    onSuccess: (cfg) => {
      setBadFields([]);
      qc.setQueryData(NIGHT_CONFIG_KEY, cfg);
      qc.invalidateQueries({ queryKey: ["nightShift"] });
      notify.success(t("saved"));
    },
    onError: (err) => {
      setBadFields(invalidConfigFields(err));
      notify.error(tn(nightErrorKey(err)));
    },
  });

  const header = (
    <div className="mb-6">
      <h2 className="text-base font-semibold" style={{ color: "var(--color-text-primary)" }}>{t("title")}</h2>
      <p className="text-sm mt-1" style={{ color: "var(--color-text-muted)", maxWidth: "72ch" }}>{t("description")}</p>
    </div>
  );

  if (q.isError) {
    return (
      <div data-testid="night-settings">
        {header}
        <p className="text-sm" style={{ color: STATUS_TEXT.warning }} data-testid="night-settings-heads-off">
          {isHeadsDisabled(q.error) ? t("headsOff") : tn("errors.unknown")}
        </p>
      </div>
    );
  }
  if (!q.data || !form) {
    return (
      <div data-testid="night-settings">
        {header}
        <Loader2 size={16} className="animate-spin" style={{ color: C.textMuted }} />
      </div>
    );
  }

  const set = (patch: NightConfigUpdate) => setForm((f) => ({ ...(f ?? {}), ...patch }));
  const localBad = [
    ...(form.start && !isHHMM(form.start) ? ["start"] : []),
    ...(form.end && !isHHMM(form.end) ? ["end"] : []),
    ...(form.start && form.end && form.start === form.end ? ["end"] : []),
    ...(form.cloud_share == null || form.cloud_share < 0 || form.cloud_share > 100 || !Number.isInteger(form.cloud_share) ? ["cloud_share"] : []),
  ];
  const bad = new Set([...badFields, ...localBad]);
  const dirty = (Object.keys(form) as (keyof NightConfigUpdate)[]).some((k) => form[k] !== q.data[k]);
  const fmt = (iso: string) =>
    new Date(iso).toLocaleString(locale, { weekday: "short", hour: "2-digit", minute: "2-digit" });
  const errText = (field: string) =>
    bad.has(field) ? (
      <p className="text-xs mt-1" style={{ color: C.error }} data-testid={`night-invalid-${field}`}>{t(`invalid.${field}`)}</p>
    ) : null;

  return (
    <motion.div
      key="night-shift"
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: -4 }}
      transition={{ duration: 0.3, ease: [0.16, 1, 0.3, 1] }}
      data-testid="night-settings"
    >
      {header}
      <div className="rounded-xl p-4 space-y-4" style={{ background: C.bgSurface, border: `1px solid ${C.border}` }}>
        <div className="flex items-center gap-4">
          <div className="flex-1 min-w-0">
            <div className="text-sm" style={{ color: "var(--color-text-primary)" }}>{t("enabled")}</div>
            <p className="text-xs mt-0.5" style={{ color: "var(--color-text-muted)" }} id="night-enabled-hint">{t("enabledHint")}</p>
          </div>
          <NightSwitch
            checked={!!form.enabled}
            onChange={(v) => set({ enabled: v })}
            label={t("enabled")}
            describedBy="night-enabled-hint"
            testId="night-enabled"
          />
        </div>

        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <label className="block">
            <span className="text-xs font-medium block mb-1.5" style={{ color: "var(--color-text-secondary)" }}>{t("start")}</span>
            <input
              type="time"
              step={60}
              value={form.start ?? ""}
              onChange={(e) => set({ start: e.target.value })}
              className={inputCls}
              style={inputStyle}
              data-testid="night-start"
              aria-invalid={bad.has("start")}
            />
            {errText("start")}
          </label>
          <label className="block">
            <span className="text-xs font-medium block mb-1.5" style={{ color: "var(--color-text-secondary)" }}>{t("end")}</span>
            <input
              type="time"
              step={60}
              value={form.end ?? ""}
              onChange={(e) => set({ end: e.target.value })}
              className={inputCls}
              style={inputStyle}
              data-testid="night-end"
              aria-invalid={bad.has("end")}
            />
            {errText("end")}
          </label>
        </div>

        <label className="block">
          <span className="text-xs font-medium block mb-1.5" style={{ color: "var(--color-text-secondary)" }}>{t("timezone")}</span>
          <select
            value={form.timezone ?? "UTC"}
            onChange={(e) => set({ timezone: e.target.value })}
            className={inputCls}
            style={inputStyle}
            data-testid="night-timezone"
            aria-invalid={bad.has("timezone")}
          >
            {(zones.includes(form.timezone ?? "") ? zones : [form.timezone ?? "UTC", ...zones]).map((z) => (
              <option key={z} value={z}>{z}</option>
            ))}
          </select>
          {deviceZone && deviceZone !== form.timezone && (
            <button
              type="button"
              onClick={() => set({ timezone: deviceZone })}
              className="mt-1 text-xs underline cursor-pointer min-h-[44px] sm:min-h-0"
              style={{ color: C.textSecondary }}
              data-testid="night-use-device-zone"
            >
              {t("useDeviceZone", { zone: deviceZone })}
            </button>
          )}
          {errText("timezone")}
        </label>

        <label className="block">
          <span className="text-xs font-medium block mb-1.5" style={{ color: "var(--color-text-secondary)" }}>{t("cloudShare")}</span>
          <input
            type="number"
            min={0}
            max={100}
            step={1}
            inputMode="numeric"
            value={form.cloud_share ?? ""}
            onChange={(e) => set({ cloud_share: e.target.value === "" ? undefined : Number(e.target.value) })}
            className={`${inputCls} sm:max-w-[140px]`}
            style={inputStyle}
            data-testid="night-cloud-share"
            aria-invalid={bad.has("cloud_share")}
          />
          <p className="text-xs mt-1" style={{ color: "var(--color-text-muted)" }}>{t("cloudShareHint")}</p>
          {errText("cloud_share")}
        </label>

        <p className="text-xs" style={{ color: C.textSecondary }} data-testid="night-next-window">
          {q.data.active
            ? t("openNow", { end: fmt(q.data.window.ends_at) })
            : t("nextWindow", { start: fmt(q.data.window.starts_at), end: fmt(q.data.window.ends_at) })}
        </p>

        <div className="flex justify-end">
          <button
            type="button"
            onClick={() => save.mutate(form)}
            disabled={!dirty || localBad.length > 0 || save.isPending}
            data-testid="night-save"
            className="inline-flex items-center justify-center gap-1.5 w-full sm:w-auto min-h-[44px] sm:min-h-[36px] px-4 rounded-md text-sm font-semibold cursor-pointer transition-colors hover:bg-[var(--color-accent-light)] disabled:opacity-40 disabled:cursor-not-allowed"
            style={{ background: C.accent, color: C.onAccent }}
          >
            {save.isPending ? <Loader2 size={14} className="animate-spin" aria-hidden /> : <Save size={14} aria-hidden />}
            {t("save")}
          </button>
        </div>
      </div>
    </motion.div>
  );
}
