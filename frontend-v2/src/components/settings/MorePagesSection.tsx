"use client";

import { useTranslations } from "next-intl";
import { LinkList } from "@/components/cockpit/LinkList";
import { sheetPages } from "@/lib/mobileNav";
import { C } from "@/lib/colors";
import type { NavItem } from "@/lib/nav";

/**
 * Settings › More — every page that left the main navigation when it became
 * Chat · Models · Knowledge (operator decision 2026-11-18). Generated from
 * lib/nav.ts (`sheetPages`), never a second list: a page added there shows up
 * here, a switched-off vertical (Benchmark) disappears here as well. Nothing
 * was deleted; this is the way to every one of them without a URL.
 */
export function MorePagesSection() {
  const t = useTranslations("cockpit.more_page");
  const tNav = useTranslations("nav");
  const labelOf = (item: NavItem) => (tNav.has(item.labelKey) ? tNav(item.labelKey) : item.label);
  const { usedMost, others } = sheetPages(labelOf);
  const items = [...usedMost, ...others].map((item) => ({ href: item.href, icon: item.icon, title: labelOf(item) }));

  return (
    <div className="flex flex-col gap-4" data-testid="settings-more">
      <div>
        <h2 className="text-base font-semibold" style={{ color: C.textPrimary }}>
          {t("title")}
        </h2>
        <p className="text-sm mt-1" style={{ color: C.textSecondary }}>
          {t("lead")}
        </p>
      </div>
      <LinkList items={items} testId="settings-more-links" />
    </div>
  );
}
