/**
 * Static job presets for the Schedule v2 JobModal.
 *
 * Clicking a template chip prefills the trigger + tag fields. The user
 * can still tweak everything afterwards.
 */

export interface JobTemplate {
  id: string;
  name: string;
  description: string;
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
    name: "Daily Standup",
    description: "Taeglicher Standup-Task fuer das Team",
    icon: "☀️",
    defaults: {
      schedule_type: "daily",
      schedule_time: "09:00",
      tags: ["morning-routine"],
    },
  },
  {
    id: "weekday-morning",
    name: "Wochentage Morgen",
    description: "Mo-Fr um 08:30 — perfekt fuer Daily Reports",
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
    name: "Weekly Cleanup",
    description: "Woechentliche Code-Qualitaets-Pruefung (So 22:00)",
    icon: "🧹",
    defaults: {
      schedule_type: "cron",
      schedule_cron: "0 22 * * 0",
      tags: ["maintenance"],
    },
  },
  {
    id: "hourly-health",
    name: "Hourly Health Check",
    description: "Stuendlicher System-Health-Check",
    icon: "🩺",
    defaults: {
      schedule_type: "interval",
      schedule_interval_hours: 1,
      tags: ["monitoring"],
    },
  },
  {
    id: "every-4h",
    name: "Alle 4 Stunden",
    description: "Periodischer Sync-Job, 6x am Tag",
    icon: "🔄",
    defaults: {
      schedule_type: "interval",
      schedule_interval_hours: 4,
      tags: ["sync"],
    },
  },
  {
    id: "monthly-report",
    name: "Monthly Report",
    description: "1. des Monats um 09:00",
    icon: "📊",
    defaults: {
      schedule_type: "cron",
      schedule_cron: "0 9 1 * *",
      tags: ["reporting"],
    },
  },
];
