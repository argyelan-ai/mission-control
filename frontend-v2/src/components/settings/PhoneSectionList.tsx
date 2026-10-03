"use client";

/**
 * Settings on the phone — one calm list of sections (icon, name, chevron),
 * grouped like the desktop side nav. Tapping a row opens that section as its
 * own screen (`/settings?section=<id>`), with a back button to this list.
 *
 * Replaces the wrapping cloud of 16 buttons that ran over four lines and off
 * the right edge (operator decision 03.10.2026). Phone only (`md:hidden`): the
 * desktop keeps its side nav, unchanged.
 *
 * Rows are links, not buttons: opening a section is navigation, so it gets a
 * history entry (the iOS back swipe returns here) and long-press works.
 */
import Link from "next/link";
import { useTranslations } from "next-intl";
import { ChevronRight } from "lucide-react";
import { C } from "@/lib/colors";
import { GROUP_ORDER, sectionHref, type SettingsSection } from "./sections";

// Same row shape as PhoneAccountPanel above it: one left edge, 48 px rows.
const rowClass =
  "w-full flex items-center gap-4 min-h-12 px-1 rounded-lg text-left cursor-pointer transition-colors hover:bg-[var(--color-bg-hover)]";

export function PhoneSectionList({
  sections,
  onOpen,
}: {
  sections: SettingsSection[];
  /** Called just before the link navigates (the page remembers the list's scroll). */
  onOpen?: (id: string) => void;
}) {
  const t = useTranslations("settings");

  return (
    <nav
      aria-label={t("phoneList.label")}
      data-testid="settings-phone-list"
      data-region="settings-phone-list"
      className="md:hidden shrink-0 px-3 pb-6"
    >
      {GROUP_ORDER.map((group) => {
        const items = sections.filter((s) => s.group === group);
        if (items.length === 0) return null;
        const headingId = `settings-phone-group-${group}`;
        return (
          // Groups by space (K8), not by boxes: twice the gap between groups
          // as between rows.
          <div key={group} className="pt-5 first:pt-2">
            <div id={headingId} className="label-sys px-1 pb-1">
              {t(`groups.${group}`)}
            </div>
            <ul aria-labelledby={headingId}>
              {items.map((section) => {
                const Icon = section.icon;
                return (
                  <li key={section.id}>
                    <Link
                      href={sectionHref(section.id)}
                      scroll={false}
                      className={rowClass}
                      data-section={section.id}
                      onClick={() => onOpen?.(section.id)}
                    >
                      <Icon size={20} aria-hidden className="shrink-0" style={{ color: C.textSecondary }} />
                      <span className="flex-1 min-w-0 truncate text-base" style={{ color: C.textPrimary }}>
                        {t(section.labelKey)}
                      </span>
                      <ChevronRight size={18} aria-hidden className="shrink-0" style={{ color: C.textMuted }} />
                    </Link>
                  </li>
                );
              })}
            </ul>
          </div>
        );
      })}
    </nav>
  );
}
