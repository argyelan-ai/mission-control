"use client";

/**
 * GroupRow — eine Gruppenzeile für die Session-Rail (Desktop) und die
 * Mobil-Liste, im Schnitt der bestehenden SessionSidebar-Zeilen.
 *
 * Nicht offensichtlich: `groupChipKind()` liefert nur die ART des Chips, nie
 * seinen Text — der kommt hier aus dem i18n-Katalog. So kann in der
 * englischen Oberfläche gar nicht erst „wartet" stehen.
 */

import { useTranslations } from "next-intl";
import { ChevronRight } from "lucide-react";
import { C } from "@/lib/colors";
import { AvatarStack } from "@/components/groupchat/AvatarStack";
import { groupChipKind } from "@/lib/groupTypes";
import type { GroupChipKind, GroupSummary } from "@/lib/groupTypes";

/** Art → i18n-Schlüssel. Eine Zuordnung, an einer Stelle. */
const CHIP_KEYS: Record<Exclude<GroupChipKind, "round">, string> = {
  waiting: "chipWaiting",
  paused: "chipPaused",
  done: "chipDone",
  failed: "chipFailed",
};

interface GroupRowProps {
  group: GroupSummary;
  selected: boolean;
  onSelect: (id: string) => void;
  /** Rail = dichte Desktop-Spalte. List = Mobil-Bildschirm mit Touch-Höhe. */
  variant?: "rail" | "list";
}

export function GroupRow({ group, selected, onSelect, variant = "rail" }: GroupRowProps) {
  const t = useTranslations("sessions.groups");
  const stack = variant === "list";

  const last = group.last_message ?? null;
  // Ohne letzte Nachricht trägt das Ziel die zweite Zeile — eine frisch
  // angelegte Gruppe soll nie mit einer leeren Zeile dastehen.
  const preview = last ? `${last.sender}: ${last.body}` : group.goal;

  const chipKind = groupChipKind(group);
  const chipText =
    chipKind === null
      ? null
      : chipKind === "round"
        ? t("chipRound", { current: group.current_round_no, max: group.max_rounds })
        : t(CHIP_KEYS[chipKind]);

  const waiting = group.status === "waiting_gate";
  // Farbe trägt hier Bedeutung, nicht Schmuck: hell = will etwas von Mark,
  // rot = kaputt, alles andere bleibt stummes Grau.
  const chipColor = waiting ? C.accent : group.status === "failed" ? C.error : C.textMuted;

  return (
    <button
      type="button"
      role="option"
      aria-selected={selected}
      onClick={() => onSelect(group.id)}
      className={`flex items-center gap-2.5 text-left w-full transition-colors cursor-pointer ${
        stack ? "px-4 min-h-[52px] py-2" : "px-3 py-2 rounded"
      }`}
      style={{
        background: selected ? C.accentSubtle : "transparent",
        borderTop: stack ? `1px solid ${C.borderSubtle}` : undefined,
      }}
    >
      <AvatarStack members={group.member_avatars ?? []} size={stack ? 22 : 20} />
      <span className="flex-1 min-w-0">
        <span
          className={`block font-medium truncate ${stack ? "text-[14px]" : "text-[13px]"}`}
          style={{ color: selected ? C.textPrimary : C.textSecondary }}
        >
          {group.name}
        </span>
        <span
          className={`block truncate ${stack ? "text-[13px] mt-0.5" : "text-[12px]"}`}
          style={{ color: C.textMuted }}
        >
          {preview}
        </span>
      </span>
      {chipText && (
        <span
          data-testid="group-chip"
          className="shrink-0 flex items-center gap-1 text-[10px] font-mono px-1.5 py-0.5 rounded"
          style={{ color: chipColor, border: `1px solid ${C.border}` }}
        >
          {waiting && (
            <span
              className="h-1.5 w-1.5 rounded-full animate-pulse"
              style={{ background: C.accent }}
              aria-hidden="true"
            />
          )}
          {chipText}
        </span>
      )}
      {stack && (
        <ChevronRight size={15} className="shrink-0" style={{ color: C.textMuted }} aria-hidden="true" />
      )}
    </button>
  );
}
