"use client";

/**
 * „Arbeitet an <Task> → Ansehen" — eine ruhige Zeile oben im Agenten-Chat.
 *
 * Ein ACP-Agent fuehrt seinen Chat und einen Task PARALLEL, jeweils in einer
 * eigenen Sitzung. Der Chat zeigt nur die Chat-Sitzung (Backend:
 * ``omp_chat.find_active_session`` folgt dem Zeiger des Chat-Daemons); was
 * der Task gerade tut, gehoert in dessen eigene Ansicht. Diese Zeile sagt,
 * DASS gerade ein Task laeuft, und fuehrt mit einem Antippen dorthin —
 * die Task-Detailansicht gibt es schon (``/tasks?task=<id>``), hier wird
 * nichts Neues gebaut.
 *
 * Die ganze Zeile ist der Link: 44 px Trefferflaeche (K11), ohne dass ein
 * eigener Knopf lauter wird als der Chat darunter (K1).
 */

import Link from "next/link";
import { ChevronRight } from "lucide-react";
import { useTranslations } from "next-intl";
import { C } from "@/lib/colors";

export interface WorkingOn {
  taskId: string;
  title: string;
}

export function WorkingOnRow({ taskId, title }: WorkingOn) {
  const t = useTranslations("sessions");
  return (
    <Link
      href={`/tasks?task=${encodeURIComponent(taskId)}`}
      data-testid="chat-working-on"
      data-region="chat-working-on"
      className="flex items-center gap-2 min-h-11 px-4 border-b shrink-0 text-xs
                 bg-[var(--color-bg-surface)] hover:bg-[var(--color-bg-hover)] transition-colors"
      style={{ borderColor: C.border }}
    >
      <span className="shrink-0" style={{ color: C.textMuted }}>
        {t("workingOn")}
      </span>
      <span className="min-w-0 flex-1 truncate" style={{ color: C.textSecondary }}>
        {title}
      </span>
      <span className="shrink-0 flex items-center gap-1" style={{ color: C.accent }}>
        {t("workingOnOpen")}
        <ChevronRight size={14} aria-hidden />
      </span>
    </Link>
  );
}
