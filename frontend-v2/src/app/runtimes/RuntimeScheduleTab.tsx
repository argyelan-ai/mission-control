// frontend-v2/src/app/runtimes/RuntimeScheduleTab.tsx
"use client";

import { useState, useEffect } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Plus, ChevronDown, Loader2, Check, X } from "lucide-react";
import { api } from "@/lib/api";
import type { RuntimeSchedule, RuntimeScheduleCreate } from "@/lib/types";
import { cn } from "@/lib/utils";
import { C, STATUS_TEXT } from "@/lib/colors";

const DAYS_LABEL: Record<string, string> = {
  daily: "täglich",
  weekdays: "Mo–Fr",
  weekends: "Sa–So",
};

const ACTION_LABEL: Record<string, string> = {
  start: "Start",
  stop: "Stop",
  kv_reset: "KV Reset",
};

function ScheduleEntry({
  schedule,
  runtimeId,
  isLmStudio,
}: {
  schedule: RuntimeSchedule;
  runtimeId: string;
  isLmStudio: boolean;
}) {
  const queryClient = useQueryClient();
  const [showRuns, setShowRuns] = useState(false);
  const [editing, setEditing] = useState(false);
  const [editError, setEditError] = useState<string | null>(null);
  const [form, setForm] = useState<RuntimeScheduleCreate>({
    name: schedule.name,
    action: schedule.action,
    time_of_day: schedule.time_of_day,
    days: schedule.days,
    unload_first: schedule.unload_first,
    enabled: schedule.enabled,
  });

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: ["runtime-schedules", runtimeId] });

  const { data: runs } = useQuery({
    queryKey: ["runtime-schedule-runs", schedule.id],
    queryFn: () => api.runtimes.schedules.runs(runtimeId, schedule.id),
    enabled: showRuns,
    staleTime: 30_000,
  });

  const updateMutation = useMutation({
    mutationFn: (data: Partial<RuntimeScheduleCreate>) =>
      api.runtimes.schedules.update(runtimeId, schedule.id, data),
    onSuccess: () => { setEditing(false); invalidate(); },
    onError: () => setEditError("Speichern fehlgeschlagen."),
  });

  const deleteMutation = useMutation({
    mutationFn: () => api.runtimes.schedules.delete(runtimeId, schedule.id),
    onSuccess: invalidate,
    onError: () => setEditError("Löschen fehlgeschlagen."),
  });

  useEffect(() => {
    if (!editing) {
      setForm({
        name: schedule.name,
        action: schedule.action,
        time_of_day: schedule.time_of_day,
        days: schedule.days,
        unload_first: schedule.unload_first,
        enabled: schedule.enabled,
      });
    }
  }, [schedule, editing]);

  const toggleEnabled = () =>
    updateMutation.mutate({ enabled: !schedule.enabled });

  const handleSave = () =>
    updateMutation.mutate(form);

  const handleDelete = () => {
    if (window.confirm(`'${schedule.name}' wirklich löschen?`)) {
      deleteMutation.mutate();
    }
  };

  const lr = schedule.last_run;

  const inputStyle: React.CSSProperties = {
    background: "rgba(255,255,255,0.06)",
    border: `1px solid ${C.borderSubtle}`,
    color: C.textPrimary,
  };

  return (
    <div
      style={{
        borderBottom: `1px solid ${C.borderSubtle}`,
        opacity: schedule.enabled ? 1 : 0.5,
      }}
    >
      {/* Main Row */}
      <div className="flex items-center justify-between gap-3 px-3 py-2.5">
        <div className="min-w-0">
          <div className="text-xs font-medium" style={{ color: C.textPrimary }}>
            {schedule.name}
          </div>
          <div className="text-xs mt-0.5" style={{ color: C.textMuted }}>
            {DAYS_LABEL[schedule.days]} {schedule.time_of_day}
            {" · "}
            <span style={{ color: schedule.action === "kv_reset" ? C.warning : "inherit" }}>
              {ACTION_LABEL[schedule.action]}
            </span>
            {schedule.unload_first && schedule.action !== "kv_reset" && " · unload-all"}
          </div>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          {/* Last run status */}
          {lr && (
            <button
              onClick={() => setShowRuns((s) => !s)}
              className="flex items-center gap-1 text-xs cursor-pointer"
              style={{ color: lr.success ? C.online : C.error }}
            >
              {lr.success ? <Check size={10} /> : <X size={10} />}
              {new Date(lr.executed_at).toLocaleTimeString("de-CH", {
                hour: "2-digit",
                minute: "2-digit",
              })}
              <ChevronDown
                size={10}
                style={{
                  transform: showRuns ? "rotate(180deg)" : undefined,
                  transition: "transform 0.15s",
                }}
              />
            </button>
          )}
          {/* Menu */}
          <div className="flex gap-1">
            <button
              onClick={() => { setEditing((e) => !e); setEditError(null); }}
              className="text-xs px-1.5 py-0.5 rounded cursor-pointer"
              style={{
                color: C.textMuted,
                background: C.borderSubtle,
              }}
            >
              {editing ? "Abbruch" : "···"}
            </button>
          </div>
        </div>
      </div>

      {/* Run History */}
      {showRuns && runs && (
        <div className="px-3 pb-2">
          <div
            className="rounded-lg overflow-hidden text-xs"
            style={{ background: "rgba(255,255,255,0.03)", border: `1px solid ${C.borderSubtle}` }}
          >
            {runs.length === 0 ? (
              <div className="px-3 py-2" style={{ color: C.textMuted }}>
                Noch keine Ausführungen.
              </div>
            ) : (
              runs.map((run, i) => (
                <div
                  key={run.executed_at}
                  className="flex items-center justify-between px-3 py-1.5"
                  style={{
                    borderBottom: i < runs.length - 1 ? `1px solid ${C.borderSubtle}` : undefined,
                  }}
                >
                  <span style={{ color: C.textMuted }}>
                    {new Date(run.executed_at).toLocaleString("de-CH", {
                      month: "2-digit",
                      day: "2-digit",
                      hour: "2-digit",
                      minute: "2-digit",
                    })}
                  </span>
                  <span style={{ color: run.success ? C.online : C.error }}>
                    {run.success ? "✓ OK" : `✗ ${run.message ?? "Fehler"}`}
                  </span>
                </div>
              ))
            )}
          </div>
        </div>
      )}

      {/* Edit Form */}
      {editing && (
        <div className="px-3 pb-3">
          <div
            className="rounded-lg p-3 flex flex-col gap-2.5"
            style={{
              background: "rgba(255,255,255,0.03)",
              border: `1px solid ${C.borderSubtle}`,
            }}
          >
            <input
              value={form.name}
              onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
              placeholder="Name"
              aria-label="Schedule Name"
              className="text-xs px-2.5 py-1.5 rounded-lg w-full"
              style={inputStyle}
            />
            <div className="flex gap-2">
              <select
                value={form.action}
                onChange={(e) => setForm((f) => ({ ...f, action: e.target.value as "start" | "stop" | "kv_reset" }))}
                aria-label="Aktion"
                className="flex-1 text-xs px-2 py-1.5 rounded-lg cursor-pointer"
                style={inputStyle}
              >
                <option value="start">Start</option>
                <option value="stop">Stop</option>
                {isLmStudio && <option value="kv_reset">KV Reset (Smart Restart)</option>}
              </select>
              <input
                type="time"
                value={form.time_of_day}
                onChange={(e) => setForm((f) => ({ ...f, time_of_day: e.target.value }))}
                aria-label="Uhrzeit"
                className="flex-1 text-xs px-2 py-1.5 rounded-lg"
                style={inputStyle}
              />
              <select
                value={form.days}
                onChange={(e) =>
                  setForm((f) => ({ ...f, days: e.target.value as "daily" | "weekdays" | "weekends" }))
                }
                aria-label="Wochentage"
                className="flex-1 text-xs px-2 py-1.5 rounded-lg cursor-pointer"
                style={inputStyle}
              >
                <option value="daily">Täglich</option>
                <option value="weekdays">Mo–Fr</option>
                <option value="weekends">Sa–So</option>
              </select>
            </div>
            {isLmStudio && form.action === "start" && (
              <label className="flex items-center gap-2 text-xs cursor-pointer" style={{ color: C.textSecondary }}>
                <input
                  type="checkbox"
                  checked={form.unload_first}
                  onChange={(e) => setForm((f) => ({ ...f, unload_first: e.target.checked }))}
                />
                Alle Modelle vorher entladen
              </label>
            )}
            {editError && (
              <div className="text-xs" style={{ color: STATUS_TEXT.error }}>{editError}</div>
            )}
            <div className="flex gap-2 justify-end">
              <button
                onClick={toggleEnabled}
                className="text-xs px-2.5 py-1 rounded-lg cursor-pointer"
                style={{
                  background: C.borderSubtle,
                  border: `1px solid ${C.borderSubtle}`,
                  color: C.textMuted,
                }}
              >
                {schedule.enabled ? "Deaktivieren" : "Aktivieren"}
              </button>
              <button
                onClick={handleDelete}
                className="text-xs px-2.5 py-1 rounded-lg cursor-pointer"
                style={{
                  background: `${C.error}14`,
                  border: `1px solid ${C.error}33`,
                  color: STATUS_TEXT.error,
                }}
              >
                Löschen
              </button>
              <button
                onClick={handleSave}
                disabled={updateMutation.isPending}
                className="text-xs px-2.5 py-1 rounded-lg cursor-pointer"
                style={{
                  background: C.accentSubtle,
                  border: `1px solid ${C.borderAccent}`,
                  color: C.accent,
                }}
              >
                {updateMutation.isPending ? <Loader2 size={10} className="animate-spin" /> : "Speichern"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function AddScheduleForm({
  runtimeId,
  isLmStudio,
  onDone,
}: {
  runtimeId: string;
  isLmStudio: boolean;
  onDone: () => void;
}) {
  const queryClient = useQueryClient();
  const [form, setForm] = useState<RuntimeScheduleCreate>({
    name: "",
    action: "start",
    time_of_day: "08:00",
    days: "daily",
    unload_first: false,
  });
  const [createError, setCreateError] = useState<string | null>(null);

  const createMutation = useMutation({
    mutationFn: () => api.runtimes.schedules.create(runtimeId, form),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["runtime-schedules", runtimeId] });
      onDone();
    },
    onError: () => setCreateError("Erstellen fehlgeschlagen."),
  });

  const inputStyle: React.CSSProperties = {
    background: "rgba(255,255,255,0.06)",
    border: `1px solid ${C.borderSubtle}`,
    color: C.textPrimary,
  };

  return (
    <div
      className="mx-3 mb-3 rounded-lg p-3 flex flex-col gap-2.5"
      style={{
        background: C.accentSubtle,
        border: `1px solid ${C.borderAccent}`,
      }}
    >
      <div className="text-xs font-medium" style={{ color: C.accent }}>
        Neuer Schedule
      </div>
      <input
        value={form.name}
        onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
        placeholder="Name (z.B. Nacht-Pause)"
        autoFocus
        aria-label="Schedule Name"
        className="text-xs px-2.5 py-1.5 rounded-lg w-full"
        style={inputStyle}
      />
      <div className="flex gap-2">
        <select
          value={form.action}
          onChange={(e) => setForm((f) => ({ ...f, action: e.target.value as "start" | "stop" | "kv_reset" }))}
          aria-label="Aktion"
          className="flex-1 text-xs px-2 py-1.5 rounded-lg cursor-pointer"
          style={inputStyle}
        >
          <option value="start">Start</option>
          <option value="stop">Stop</option>
          {isLmStudio && <option value="kv_reset">KV Reset (Smart Restart)</option>}
        </select>
        <input
          type="time"
          value={form.time_of_day}
          onChange={(e) => setForm((f) => ({ ...f, time_of_day: e.target.value }))}
          aria-label="Uhrzeit"
          className="flex-1 text-xs px-2 py-1.5 rounded-lg"
          style={inputStyle}
        />
        <select
          value={form.days}
          onChange={(e) =>
            setForm((f) => ({ ...f, days: e.target.value as "daily" | "weekdays" | "weekends" }))
          }
          aria-label="Wochentage"
          className="flex-1 text-xs px-2 py-1.5 rounded-lg cursor-pointer"
          style={inputStyle}
        >
          <option value="daily">Täglich</option>
          <option value="weekdays">Mo–Fr</option>
          <option value="weekends">Sa–So</option>
        </select>
      </div>
      {isLmStudio && form.action === "start" && (
        <label className="flex items-center gap-2 text-xs cursor-pointer" style={{ color: C.textSecondary }}>
          <input
            type="checkbox"
            checked={form.unload_first}
            onChange={(e) => setForm((f) => ({ ...f, unload_first: e.target.checked }))}
          />
          Alle Modelle vorher entladen
        </label>
      )}
      {createError && (
        <div className="text-xs" style={{ color: STATUS_TEXT.error }}>{createError}</div>
      )}
      <div className="flex gap-2 justify-end">
        <button
          onClick={onDone}
          className="text-xs px-2.5 py-1 rounded-lg cursor-pointer"
          style={{
            background: C.borderSubtle,
            border: `1px solid ${C.borderSubtle}`,
            color: C.textMuted,
          }}
        >
          Abbrechen
        </button>
        <button
          onClick={() => createMutation.mutate()}
          disabled={!form.name.trim() || createMutation.isPending}
          className="text-xs px-2.5 py-1 rounded-lg cursor-pointer"
          style={{
            background: C.accentSubtle,
            border: `1px solid ${C.borderAccent}`,
            color: C.accent,
          }}
        >
          {createMutation.isPending ? <Loader2 size={10} className="animate-spin" /> : "Speichern"}
        </button>
      </div>
    </div>
  );
}

export function RuntimeScheduleTab({
  runtimeId,
  runtimeType,
}: {
  runtimeId: string;
  runtimeType: string;
}) {
  const [showForm, setShowForm] = useState(false);
  const isLmStudio = runtimeType === "lmstudio";

  const { data: schedules, isLoading, isError } = useQuery({
    queryKey: ["runtime-schedules", runtimeId],
    queryFn: () => api.runtimes.schedules.list(runtimeId),
    refetchInterval: 30_000,
  });

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-6">
        <Loader2 size={14} className="animate-spin" style={{ color: C.textMuted }} />
      </div>
    );
  }

  if (isError) {
    return (
      <div className="px-3 py-4 text-xs text-center" style={{ color: STATUS_TEXT.error }}>
        Fehler beim Laden der Schedules.
      </div>
    );
  }

  return (
    <div>
      {/* Schedule List */}
      {schedules && schedules.length > 0 ? (
        <div
          className="mx-3 mt-3 rounded-lg overflow-hidden"
          style={{ border: `1px solid ${C.borderSubtle}` }}
        >
          {schedules.map((s) => (
            <ScheduleEntry
              key={s.id}
              schedule={s}
              runtimeId={runtimeId}
              isLmStudio={isLmStudio}
            />
          ))}
        </div>
      ) : (
        !showForm && (
          <div
            className="mx-3 mt-3 py-4 text-center text-xs rounded-lg"
            style={{ color: C.textMuted, border: `1px dashed ${C.border}` }}
          >
            Noch keine Schedules konfiguriert.
          </div>
        )
      )}

      {/* Add Form */}
      {showForm ? (
        <div className="mt-3">
          <AddScheduleForm
            runtimeId={runtimeId}
            isLmStudio={isLmStudio}
            onDone={() => setShowForm(false)}
          />
        </div>
      ) : (
        <button
          onClick={() => setShowForm(true)}
          className={cn(
            "w-full text-xs py-2 mt-2 cursor-pointer flex items-center justify-center gap-1.5"
          )}
          style={{ color: C.textMuted }}
        >
          <Plus size={11} />
          Schedule hinzufügen
        </button>
      )}
    </div>
  );
}
