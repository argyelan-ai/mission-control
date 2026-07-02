"use client";

/**
 * TriggerEditor — schedule trigger configuration UI.
 *
 * Used inside JobModal but extracted because the same widget will pop
 * up in inline-job-edit flows too. Stateless: parent owns the schedule
 * fields and patches via onChange.
 */

import { useMemo } from "react";
import { Calendar, Clock, Repeat, Code2, Sliders } from "lucide-react";
import cronstrue from "cronstrue";
import { C } from "@/lib/colors";

export interface TriggerEditorValue {
  schedule_type: string;
  schedule_time?: string | null;
  schedule_cron?: string | null;
  schedule_weekdays?: number[] | null;
  schedule_interval_hours?: number | null;
  start_date?: string | null;
  end_date?: string | null;
}

interface TriggerEditorProps {
  schedule_type: string;
  schedule_time?: string;
  schedule_cron?: string;
  schedule_weekdays?: number[];
  schedule_interval_hours?: number;
  start_date?: string;
  end_date?: string;
  onChange: (fields: Partial<TriggerEditorValue>) => void;
  /** ISO datetimes returned by api.schedule.previewFirings */
  firingPreview?: string[];
  onPreviewRequest?: () => void;
}

type TriggerKind =
  | "daily"
  | "weekdays"
  | "interval"
  | "cron"
  | "weekly_custom";

const KIND_OPTIONS: Array<{
  id: TriggerKind;
  label: string;
  icon: typeof Calendar;
}> = [
  { id: "daily", label: "Taeglich", icon: Calendar },
  { id: "weekdays", label: "Wochentage", icon: Clock },
  { id: "interval", label: "Intervall", icon: Repeat },
  { id: "cron", label: "Cron", icon: Code2 },
  { id: "weekly_custom", label: "Benutzerdef.", icon: Sliders },
];

const DAY_LABELS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"];

function deriveKind(t: string): TriggerKind {
  if (t === "weekly_custom") return "weekly_custom";
  if (t === "cron") return "cron";
  if (t === "interval") return "interval";
  if (t === "weekdays") return "weekdays";
  return "daily";
}

function formatPreviewDate(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const dd = String(d.getDate()).padStart(2, "0");
  const mo = String(d.getMonth() + 1).padStart(2, "0");
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  return `${dd}.${mo} ${hh}:${mm}`;
}

export function TriggerEditor(props: TriggerEditorProps) {
  const {
    schedule_type,
    schedule_time,
    schedule_cron,
    schedule_weekdays,
    schedule_interval_hours,
    start_date,
    end_date,
    onChange,
    firingPreview,
  } = props;

  const kind = deriveKind(schedule_type);
  const weekdays = schedule_weekdays ?? [];

  const cronDescription = useMemo(() => {
    if (kind !== "cron" || !schedule_cron) return null;
    try {
      return cronstrue.toString(schedule_cron, { locale: "de" });
    } catch (e) {
      return `Ungueltig: ${(e as Error).message}`;
    }
  }, [kind, schedule_cron]);

  const setKind = (next: TriggerKind) => {
    if (next === "daily") {
      onChange({
        schedule_type: "daily",
        schedule_time: schedule_time ?? "09:00",
        schedule_cron: null,
        schedule_weekdays: null,
        schedule_interval_hours: null,
      });
    } else if (next === "weekdays") {
      onChange({
        schedule_type: "weekdays",
        schedule_time: schedule_time ?? "09:00",
        schedule_weekdays: [0, 1, 2, 3, 4],
        schedule_cron: null,
        schedule_interval_hours: null,
      });
    } else if (next === "interval") {
      onChange({
        schedule_type: "interval",
        schedule_interval_hours: schedule_interval_hours ?? 1,
        schedule_time: null,
        schedule_cron: null,
        schedule_weekdays: null,
      });
    } else if (next === "cron") {
      onChange({
        schedule_type: "cron",
        schedule_cron: schedule_cron ?? "0 9 * * *",
        schedule_time: null,
        schedule_interval_hours: null,
        schedule_weekdays: null,
      });
    } else {
      onChange({
        schedule_type: "weekly_custom",
        schedule_time: schedule_time ?? "09:00",
        schedule_weekdays: schedule_weekdays ?? [0, 2, 4],
        schedule_cron: null,
        schedule_interval_hours: null,
      });
    }
  };

  const toggleWeekday = (idx: number) => {
    const cur = new Set(weekdays);
    if (cur.has(idx)) cur.delete(idx);
    else cur.add(idx);
    onChange({ schedule_weekdays: Array.from(cur).sort((a, b) => a - b) });
  };

  const inputStyle: React.CSSProperties = {
    background: C.bgSurface,
    border: `1px solid ${C.border}`,
    color: C.textPrimary,
  };

  return (
    <div className="flex flex-col gap-4">
      {/* Segmented switcher */}
      <div
        className="flex flex-wrap gap-1 rounded-lg p-1"
        style={{ border: `1px solid ${C.border}`, background: C.bgSurface }}
      >
        {KIND_OPTIONS.map((opt) => {
          const Icon = opt.icon;
          const active = kind === opt.id;
          return (
            <button
              key={opt.id}
              type="button"
              onClick={() => setKind(opt.id)}
              className="flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-xs font-medium transition"
              style={{
                background: active ? C.accentSubtle : "transparent",
                color: active ? C.accent : C.textSecondary,
              }}
            >
              <Icon size={12} />
              {opt.label}
            </button>
          );
        })}
      </div>

      {/* Per-kind body */}
      {kind === "daily" && (
        <Field label="Uhrzeit">
          <TimeInput
            value={schedule_time ?? ""}
            onChange={(v) => onChange({ schedule_time: v })}
          />
        </Field>
      )}

      {kind === "weekdays" && (
        <div className="flex flex-col gap-2">
          <Field label="Uhrzeit">
            <TimeInput
              value={schedule_time ?? ""}
              onChange={(v) => onChange({ schedule_time: v })}
            />
          </Field>
          <p className="text-[11px]" style={{ color: C.textMuted }}>
            Laeuft Mo, Di, Mi, Do, Fr — Wochenende ausgenommen.
          </p>
        </div>
      )}

      {kind === "interval" && (
        <Field label="Intervall (Stunden)">
          <div className="flex items-center gap-2">
            <input
              type="number"
              min={1}
              max={168}
              value={schedule_interval_hours ?? 1}
              onChange={(e) =>
                onChange({
                  schedule_interval_hours: Math.max(1, Number(e.target.value)),
                })
              }
              className="w-24 rounded-md px-2 py-1.5 text-sm"
              style={inputStyle}
              aria-label="Intervall in Stunden"
            />
            <span className="text-xs" style={{ color: C.textSecondary }}>Stunden</span>
          </div>
        </Field>
      )}

      {kind === "cron" && (
        <div className="flex flex-col gap-2">
          <Field label="Cron-Expression">
            <input
              type="text"
              value={schedule_cron ?? ""}
              onChange={(e) => onChange({ schedule_cron: e.target.value })}
              placeholder="0 9 * * 1-5"
              className="w-full rounded-md px-2.5 py-1.5 font-mono text-sm"
              style={inputStyle}
              aria-label="Cron-Expression"
            />
          </Field>
          {cronDescription && (
            <div
              className="flex items-start gap-2 rounded-md px-3 py-2 text-[11px]"
              style={{ border: `1px solid ${C.borderSubtle}`, background: C.bgBase }}
            >
              <Code2 size={12} className="mt-0.5 shrink-0" style={{ color: C.accent }} />
              <span style={{ color: C.textSecondary }}>{cronDescription}</span>
            </div>
          )}
        </div>
      )}

      {kind === "weekly_custom" && (
        <div className="flex flex-col gap-3">
          <Field label="Uhrzeit">
            <TimeInput
              value={schedule_time ?? ""}
              onChange={(v) => onChange({ schedule_time: v })}
            />
          </Field>
          <div className="flex flex-col gap-1.5">
            <span className="text-[10px] uppercase tracking-wide" style={{ color: C.textDim }}>
              Wochentage
            </span>
            <div className="flex flex-wrap gap-1.5">
              {DAY_LABELS.map((lbl, i) => {
                const active = weekdays.includes(i);
                return (
                  <button
                    key={lbl}
                    type="button"
                    onClick={() => toggleWeekday(i)}
                    className="rounded-md border px-2.5 py-1 text-xs font-medium transition"
                    style={{
                      borderColor: active ? C.borderAccent : "rgba(255,255,255,0.08)",
                      background: active ? C.accentSubtle : "transparent",
                      color: active ? C.accent : C.textSecondary,
                    }}
                  >
                    {lbl}
                  </button>
                );
              })}
            </div>
          </div>
        </div>
      )}

      {/* Firing preview — shown for all trigger types when available */}
      {firingPreview && firingPreview.length > 0 && (
        <div
          className="flex flex-col gap-1 rounded-md px-3 py-2"
          style={{ border: `1px solid ${C.borderSubtle}`, background: C.bgBase }}
        >
          <span className="text-[10px] uppercase tracking-wide" style={{ color: C.textDim }}>
            Nächste {firingPreview.length} Firings
          </span>
          <ul className="flex flex-col gap-0.5 text-[11px] font-mono" style={{ color: C.textSecondary }}>
            {firingPreview.slice(0, 5).map((iso) => (
              <li key={iso}>{formatPreviewDate(iso)}</li>
            ))}
          </ul>
        </div>
      )}

      {/* Active range */}
      <div className="flex flex-col gap-1.5 pt-3" style={{ borderTop: `1px solid ${C.borderSubtle}` }}>
        <span className="text-[10px] uppercase tracking-wide" style={{ color: C.textDim }}>
          Nur aktiv von … bis (optional)
        </span>
        <div className="flex flex-wrap items-center gap-2">
          <DateInput
            value={start_date ?? ""}
            onChange={(v) => onChange({ start_date: v || null })}
          />
          <span className="text-xs" style={{ color: C.textDim }}>bis</span>
          <DateInput
            value={end_date ?? ""}
            onChange={(v) => onChange({ end_date: v || null })}
          />
          {(start_date || end_date) && (
            <button
              type="button"
              onClick={() =>
                onChange({ start_date: null, end_date: null })
              }
              className="text-[10px]"
              style={{ color: C.textMuted }}
            >
              zuruecksetzen
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

function Field({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <span className="text-[10px] uppercase tracking-wide" style={{ color: C.textDim }}>
        {label}
      </span>
      {children}
    </div>
  );
}

function TimeInput({
  value,
  onChange,
}: {
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <input
      type="time"
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className="w-32 rounded-md px-2.5 py-1.5 text-sm"
      style={{
        background: C.bgSurface,
        border: `1px solid ${C.border}`,
        color: C.textPrimary,
        colorScheme: "dark",
      }}
      aria-label="Uhrzeit"
    />
  );
}

function DateInput({
  value,
  onChange,
}: {
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <input
      type="date"
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className="rounded-md px-2.5 py-1.5 text-sm"
      style={{
        background: C.bgSurface,
        border: `1px solid ${C.border}`,
        color: C.textPrimary,
        colorScheme: "dark",
      }}
      aria-label="Datum"
    />
  );
}
