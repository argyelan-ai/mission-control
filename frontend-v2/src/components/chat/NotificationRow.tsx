"use client";

/**
 * Eine Hintergrund-Meldung der CLI als schmale Zeile.
 *
 * Gezeigt wird sie NUR, wenn sie zu keinem Werkzeugaufruf im Verlauf gehoert —
 * also fuer Hintergrund-Befehle. Meldungen zu einem Subagenten wandern in
 * dessen Karte (siehe `notificationsByTool`), damit dieselbe Aussage nicht
 * zweimal nebeneinander steht.
 *
 * Was die CLI schickt, ist eine Wand aus Kennungen und einem Host-Pfad. Hier
 * steht nur, was ein Mensch davon braucht: was passiert ist und wie es
 * ausging. Den Pfad reicht das Backend gar nicht erst durch.
 */

import { CheckCircle2, XCircle } from "lucide-react";
import { useTranslations } from "next-intl";
import { C, STATUS } from "@/lib/colors";
import type { NotificationEvent } from "@/lib/chatTypes";

export function NotificationRow({ ev }: { ev: NotificationEvent }) {
  const t = useTranslations("sessions");
  const failed = ev.status === "failed";
  const Icon = failed ? XCircle : CheckCircle2;

  return (
    <div className="w-full px-4 md:px-5 py-1">
      <div className="flex items-center gap-2 text-[12px]" style={{ color: C.textMuted }}>
        <Icon size={12} className="shrink-0" style={{ color: failed ? STATUS.error : C.textMuted }} />
        <span className="truncate">
          {ev.summary ?? (failed ? t("notificationFailed") : t("notificationDone"))}
        </span>
      </div>
    </div>
  );
}
