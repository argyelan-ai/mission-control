// frontend-v2/src/app/runtimes/RuntimeScheduleTab.tsx
"use client";

import { useState, useEffect } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Plus, ChevronDown, Loader2, Check, X } from "lucide-react";
import { useTranslations } from "next-intl";
import { api } from "@/lib/api";
import type { RuntimeSchedule, RuntimeScheduleCreate } from "@/lib/types";
import { ConfirmDialog } from "@/components/shared/ConfirmDialog";
import { cn } from "@/lib/utils";
import { C, STATUS_TEXT } from "@/lib/colors";

// labelKey pattern (docs/i18n.md): resolved via t() at the render site.
const DAYS_BADGE_KEY: Record<string, string> = {
  daily: "daysBadgeDaily",
  weekdays: "daysBadgeWeekdays",
  weekends: "daysBadgeWeekends",
};

const ACTION_BADGE_KEY: Record<string, string> = {
  start: "actionBadgeStart",
  stop: "actionBadgeStop",
  kv_reset: "actionBadgeKvReset",
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
  const t = useTranslations("runtimes.scheduleTab");
  const queryClient = useQueryClient();
  const [showRuns, setShowRuns] = useState(false);
  const [editing, setEditing] = useState(false);
  const [editError, setEditError] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);
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
    onError: () => setEditError(t("saveFailed")),
  });

  const deleteMutation = useMutation({
    mutationFn: () => api.runtimes.schedules.delete(runtimeId, schedule.id),
    onSuccess: invalidate,
    onError: () => setEditError(t("deleteFailed")),
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
    setConfirmDelete(true);
  };

  const lr = schedule.last_run;

  const inputStyle: React.CSSProperties = {
    background: "var(--color-bg-elevated)",
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
            {t(DAYS_BADGE_KEY[schedule.days])} {schedule.time_of_day}
            {" · "}
            <span style={{ color: schedule.action === "kv_reset" ? C.warning : "inherit" }}>
              {t(ACTION_BADGE_KEY[schedule.action])}
            </span>
            {schedule.unload_first && schedule.action !== "kv_reset" && ` · ${t("unloadAllBadge")}`}
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
              {editing ? t("cancel") : "···"}
            </button>
          </div>
        </div>
      </div>

      {/* Run History */}
      {showRuns && runs && (
        <div className="px-3 pb-2">
          <div
            className="rounded-lg overflow-hidden text-xs"
            style={{ background: "var(--color-bg-surface)", border: `1px solid ${C.borderSubtle}` }}
          >
            {runs.length === 0 ? (
              <div className="px-3 py-2" style={{ color: C.textMuted }}>
                {t("noRunsYet")}
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
                    {run.success ? t("runOk") : t("runError", { message: run.message ?? t("errorFallback") })}
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
              background: "var(--color-bg-surface)",
              border: `1px solid ${C.borderSubtle}`,
            }}
          >
            <input
              value={form.name}
              onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
              placeholder={t("namePlaceholder")}
              aria-label={t("scheduleNameAria")}
              className="text-xs px-2.5 py-1.5 rounded-lg w-full"
              style={inputStyle}
            />
            <div className="flex gap-2">
              <select
                value={form.action}
                onChange={(e) => setForm((f) => ({ ...f, action: e.target.value as "start" | "stop" | "kv_reset" }))}
                aria-label={t("actionAria")}
                className="flex-1 text-xs px-2 py-1.5 rounded-lg cursor-pointer"
                style={inputStyle}
              >
                <option value="start">{t("actionOptionStart")}</option>
                <option value="stop">{t("actionOptionStop")}</option>
                {isLmStudio && <option value="kv_reset">{t("actionOptionKvReset")}</option>}
              </select>
              <input
                type="time"
                value={form.time_of_day}
                onChange={(e) => setForm((f) => ({ ...f, time_of_day: e.target.value }))}
                aria-label={t("timeAria")}
                className="flex-1 text-xs px-2 py-1.5 rounded-lg"
                style={inputStyle}
              />
              <select
                value={form.days}
                onChange={(e) =>
                  setForm((f) => ({ ...f, days: e.target.value as "daily" | "weekdays" | "weekends" }))
                }
                aria-label={t("daysAria")}
                className="flex-1 text-xs px-2 py-1.5 rounded-lg cursor-pointer"
                style={inputStyle}
              >
                <option value="daily">{t("daysOptionDaily")}</option>
                <option value="weekdays">{t("daysOptionWeekdays")}</option>
                <option value="weekends">{t("daysOptionWeekends")}</option>
              </select>
            </div>
            {isLmStudio && form.action === "start" && (
              <label className="flex items-center gap-2 text-xs cursor-pointer" style={{ color: C.textSecondary }}>
                <input
                  type="checkbox"
                  checked={form.unload_first}
                  onChange={(e) => setForm((f) => ({ ...f, unload_first: e.target.checked }))}
                />
                {t("unloadAllFirst")}
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
                {schedule.enabled ? t("disable") : t("enable")}
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
                {t("delete")}
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
                {updateMutation.isPending ? <Loader2 size={10} className="animate-spin" /> : t("save")}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* v3 confirm — replaces native window.confirm() (panel register rule 3) */}
      <ConfirmDialog
        open={confirmDelete}
        title={t("deleteScheduleTitle")}
        body={t("deleteScheduleBody", { name: schedule.name })}
        confirmLabel={t("delete")}
        loading={deleteMutation.isPending}
        onConfirm={() =>
          deleteMutation.mutate(undefined, {
            onSettled: () => setConfirmDelete(false),
          })
        }
        onCancel={() => setConfirmDelete(false)}
      />
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
  const t = useTranslations("runtimes.scheduleTab");
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
    onError: () => setCreateError(t("createFailed")),
  });

  const inputStyle: React.CSSProperties = {
    background: "var(--color-bg-elevated)",
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
        {t("newSchedule")}
      </div>
      <input
        value={form.name}
        onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
        placeholder={t("nameExamplePlaceholder")}
        autoFocus
        aria-label={t("scheduleNameAria")}
        className="text-xs px-2.5 py-1.5 rounded-lg w-full"
        style={inputStyle}
      />
      <div className="flex gap-2">
        <select
          value={form.action}
          onChange={(e) => setForm((f) => ({ ...f, action: e.target.value as "start" | "stop" | "kv_reset" }))}
          aria-label={t("actionAria")}
          className="flex-1 text-xs px-2 py-1.5 rounded-lg cursor-pointer"
          style={inputStyle}
        >
          <option value="start">{t("actionOptionStart")}</option>
          <option value="stop">{t("actionOptionStop")}</option>
          {isLmStudio && <option value="kv_reset">{t("actionOptionKvReset")}</option>}
        </select>
        <input
          type="time"
          value={form.time_of_day}
          onChange={(e) => setForm((f) => ({ ...f, time_of_day: e.target.value }))}
          aria-label={t("timeAria")}
          className="flex-1 text-xs px-2 py-1.5 rounded-lg"
          style={inputStyle}
        />
        <select
          value={form.days}
          onChange={(e) =>
            setForm((f) => ({ ...f, days: e.target.value as "daily" | "weekdays" | "weekends" }))
          }
          aria-label={t("daysAria")}
          className="flex-1 text-xs px-2 py-1.5 rounded-lg cursor-pointer"
          style={inputStyle}
        >
          <option value="daily">{t("daysOptionDaily")}</option>
          <option value="weekdays">{t("daysOptionWeekdays")}</option>
          <option value="weekends">{t("daysOptionWeekends")}</option>
        </select>
      </div>
      {isLmStudio && form.action === "start" && (
        <label className="flex items-center gap-2 text-xs cursor-pointer" style={{ color: C.textSecondary }}>
          <input
            type="checkbox"
            checked={form.unload_first}
            onChange={(e) => setForm((f) => ({ ...f, unload_first: e.target.checked }))}
          />
          {t("unloadAllFirst")}
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
          {t("cancel")}
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
          {createMutation.isPending ? <Loader2 size={10} className="animate-spin" /> : t("save")}
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
  const t = useTranslations("runtimes.scheduleTab");
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
        {t("loadError")}
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
            {t("noSchedulesYet")}
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
          {t("addSchedule")}
        </button>
      )}
    </div>
  );
}
