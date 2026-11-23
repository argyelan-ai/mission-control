"use client";

import { useTranslations } from "next-intl";
import { LinkList } from "@/components/cockpit/LinkList";
import { sheetPages } from "@/lib/mobileNav";
import { C } from "@/lib/colors";
import type { NavItem } from "@/lib/nav";

/**
 * The body of the More page (/more): every page that left the main
 * navigation when it became Chat · Models · Knowledge (operator decision
 * 2026-11-18), in two titled groups — "Used most" and "Others".
 *
 * Generated from lib/nav.ts (`sheetPages`), never a second list: a page added
 * there shows up here, a switched-off vertical (Benchmark) disappears here as
 * well, and a page a tab already reaches (Memory, Skills under Knowledge) is
 * not repeated. Nothing was deleted; this is the way to every one of them
 * without a URL.
 */
export function MorePagesSection() {
  const t = useTranslations("cockpit.more_page");
  const tNav = useTranslations("nav");
  const labelOf = (item: NavItem) => (tNav.has(item.labelKey) ? tNav(item.labelKey) : item.label);
  const { usedMost, others } = sheetPages(labelOf);
  const toItems = (items: NavItem[]) => items.map((item) => ({ href: item.href, icon: item.icon, title: labelOf(item) }));
  const groups = [
    { id: "used", title: t("usedMost"), items: usedMost },
    { id: "others", title: t("others"), items: others },
  ].filter((g) => g.items.length > 0);

  return (
    <div className="flex flex-col gap-8" data-testid="more-pages">
      {groups.map((g) => (
        <section key={g.id} aria-labelledby={`more-${g.id}`} className="flex flex-col gap-2">
          <h2 id={`more-${g.id}`} className="text-sm font-medium" style={{ color: C.textMuted }}>
            {g.title}
          </h2>
          <LinkList items={toItems(g.items)} testId={`more-${g.id}-links`} />
        </section>
      ))}
    </div>
  );
}
