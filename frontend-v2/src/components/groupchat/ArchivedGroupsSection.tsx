"use client";

/**
 * ArchivedGroupsSection — der Archiv-Bereich am Fuss der Session-Liste
 * (ADR-075, Nachtrag): archivierte Gruppen sind weggeräumt, nicht weg.
 *
 * Bewusst leiser als die aktiven Zeilen: zugeklappt als Grundzustand, keine
 * Status-Chips, keine Vorschau — wer hier sucht, sucht einen Namen. Und ohne
 * archivierte Gruppen rendert die Sektion GAR nichts: ein leerer
 * „Archiv"-Kasten würde jede Sidebar mit einem toten Element belasten.
 *
 * Öffnen und Zurückholen sind GESCHWISTER-Buttons, nicht verschachtelt —
 * verschachtelte Buttons sind invalides HTML, und so braucht das Zurückholen
 * auch kein stopPropagation.
 */
import { useState } from "react";
import { useTranslations } from "next-intl";
import { ArchiveRestore, ChevronDown, ChevronRight } from "lucide-react";
import { C } from "@/lib/colors";
import { AvatarStack } from "@/components/groupchat/AvatarStack";
import type { GroupSummary } from "@/lib/groupTypes";

interface ArchivedGroupsSectionProps {
  /** Nur archivierte Gruppen — der Aufrufer filtert (archived_at gesetzt). */
  groups: GroupSummary[];
  selectedGroupId: string | null;
  onSelectGroup: (id: string) => void;
  onUnarchive: (id: string) => void;
  /** Rail = dichte Desktop-Spalte. List = Mobil-Bildschirm mit Touch-Höhen. */
  variant?: "rail" | "list";
}

export function ArchivedGroupsSection({
  groups,
  selectedGroupId,
  onSelectGroup,
  onUnarchive,
  variant = "rail",
}: ArchivedGroupsSectionProps) {
  const t = useTranslations("sessions.groups");
  const [open, setOpen] = useState(false);
  const stack = variant === "list";

  if (groups.length === 0) return null;

  const Chevron = open ? ChevronDown : ChevronRight;

  return (
    <div data-testid="archived-groups-section">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className={`flex items-center gap-1.5 w-full text-left cursor-pointer transition-colors ${
          stack ? "px-4 min-h-[44px]" : "px-3 py-1.5 rounded-md"
        }`}
      >
        <Chevron size={13} className="shrink-0" style={{ color: C.textDim }} aria-hidden="true" />
        <span className="label-sys truncate" style={{ color: C.textMuted }}>
          {t("archivedSection")}
        </span>
        <span className="text-[11px] tabular-nums" style={{ color: C.textDim }} aria-hidden="true">
          {groups.length}
        </span>
      </button>
      {open && (
        <div className="flex flex-col">
          {groups.map((group) => {
            const selected = group.id === selectedGroupId;
            return (
              <div
                key={group.id}
                className={`flex items-center ${
                  stack ? "pl-4 pr-1 min-h-[52px]" : "pl-3 pr-0.5 rounded-lg"
                }`}
                style={{
                  background: selected ? C.accentSubtle : "transparent",
                  borderTop: stack ? `1px solid ${C.borderSubtle}` : undefined,
                }}
              >
                <button
                  type="button"
                  role="option"
                  aria-selected={selected}
                  onClick={() => onSelectGroup(group.id)}
                  className="flex items-center gap-2.5 flex-1 min-w-0 text-left py-2 cursor-pointer"
                >
                  {/* Avatare gedimmt: hier arbeitet niemand mehr — sie helfen
                      nur beim Wiedererkennen, nicht beim Hinschauen. */}
                  <span className="opacity-60 shrink-0" aria-hidden="true">
                    <AvatarStack members={group.member_avatars ?? []} size={stack ? 22 : 20} />
                  </span>
                  <span
                    className={`block truncate ${stack ? "text-[14px]" : "text-[13px]"}`}
                    style={{ color: selected ? C.textSecondary : C.textMuted }}
                  >
                    {group.name}
                  </span>
                </button>
                <button
                  type="button"
                  onClick={() => onUnarchive(group.id)}
                  aria-label={t("unarchive")}
                  title={t("unarchive")}
                  className={`flex items-center justify-center shrink-0 rounded-md cursor-pointer transition-colors ${
                    stack ? "w-11 h-11" : "w-8 h-8"
                  }`}
                  style={{ color: C.textMuted }}
                >
                  <ArchiveRestore size={stack ? 17 : 15} />
                </button>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
