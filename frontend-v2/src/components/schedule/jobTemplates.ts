/**
 * Static job presets for the Schedule v2 JobModal.
 *
 * Clicking a template chip prefills the trigger + tag fields. The user
 * can still tweak everything afterwards.
 *
 * `key` resolves to `schedule.jobTemplates.<key>.name` / `.description` in
 * the message catalogs (messages/en.json + messages/de.json) — render sites
 * must call `t()`, never read a hardcoded label off this array.
 */

export interface JobTemplate {
  id: string;
  key: string;
  icon: string; // emoji
  defaults: Partial<{
    schedule_type: string;
    schedule_time: string;
    schedule_cron: string;
    schedule_interval_hours: number;
    schedule_weekdays: number[];
    tags: string[];
    task_payload: Record<string, unknown>;
  }>;
}

export const JOB_TEMPLATES: JobTemplate[] = [
  {
    id: "daily-standup",
    key: "dailyStandup",
    icon: "☀️",
    defaults: {
      schedule_type: "daily",
      schedule_time: "09:00",
      tags: ["morning-routine"],
    },
  },
  {
    id: "weekday-morning",
    key: "weekdayMorning",
    icon: "🗓️",
    defaults: {
      schedule_type: "weekdays",
      schedule_time: "08:30",
      schedule_weekdays: [0, 1, 2, 3, 4],
      tags: ["weekday-routine"],
    },
  },
  {
    id: "weekly-cleanup",
    key: "weeklyCleanup",
    icon: "🧹",
    defaults: {
      schedule_type: "cron",
      schedule_cron: "0 22 * * 0",
      tags: ["maintenance"],
    },
  },
  {
    id: "hourly-health",
    key: "hourlyHealth",
    icon: "🩺",
    defaults: {
      schedule_type: "interval",
      schedule_interval_hours: 1,
      tags: ["monitoring"],
    },
  },
  {
    id: "every-4h",
    key: "every4h",
    icon: "🔄",
    defaults: {
      schedule_type: "interval",
      schedule_interval_hours: 4,
      tags: ["sync"],
    },
  },
  {
    id: "monthly-report",
    key: "monthlyReport",
    icon: "📊",
    defaults: {
      schedule_type: "cron",
      schedule_cron: "0 9 1 * *",
      tags: ["reporting"],
    },
  },
];
