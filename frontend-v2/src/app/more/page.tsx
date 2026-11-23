"use client";

import { useTranslations } from "next-intl";
import AppShell from "@/components/layout/AppShell";
import { PAGE_READ } from "@/components/layout/AppShellWidth";
import { MorePagesSection } from "@/components/settings/MorePagesSection";
import { C } from "@/lib/colors";

/**
 * More — its own page with its own title. As a Settings section it carried
 * the Settings head and a back link to Settings, which read as "this is a
 * setting". `/settings?section=more` (old links) forwards here.
 */
export default function MorePage() {
  const t = useTranslations("cockpit.more_page");
  return (
    <AppShell mobileTitle={t("title")}>
      <div className={`${PAGE_READ} flex flex-col gap-8`} data-region="more" data-testid="more-page">
        <header>
          {/* Phone: the title stands in the app bar (like Knowledge). */}
          <h1 className="hidden md:block display text-2xl font-semibold" style={{ color: C.textPrimary }}>
            {t("title")}
          </h1>
          <p className="text-sm md:mt-1" style={{ color: C.textSecondary }}>
            {t("lead")}
          </p>
        </header>
        <MorePagesSection />
      </div>
    </AppShell>
  );
}
