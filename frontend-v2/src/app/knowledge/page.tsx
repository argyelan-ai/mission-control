"use client";

import { useTranslations } from "next-intl";
import { Brain, Puzzle } from "lucide-react";
import AppShell from "@/components/layout/AppShell";
import { LinkList } from "@/components/cockpit/LinkList";
import { C } from "@/lib/colors";

/**
 * Knowledge — the third tab (operator decision 2026-11-18). A landing, not a
 * rebuild: it leads to the existing Memory and Skills pages. ADR-102
 * principle 6 (facts, skills, project knowledge in one place) reshapes it
 * later; until then it is the one door to both.
 */
export default function KnowledgePage() {
  const t = useTranslations("cockpit.knowledge");
  return (
    <AppShell>
      <div className="max-w-2xl mx-auto w-full flex flex-col gap-6" data-region="knowledge">
        <header>
          <h1 className="display text-2xl font-semibold" style={{ color: C.textPrimary }}>
            {t("title")}
          </h1>
          <p className="text-sm mt-1" style={{ color: C.textSecondary }}>
            {t("lead")}
          </p>
        </header>
        <LinkList
          testId="knowledge-links"
          items={[
            { href: "/memory", icon: Brain, title: t("memoryTitle"), hint: t("memoryHint") },
            { href: "/skills", icon: Puzzle, title: t("skillsTitle"), hint: t("skillsHint") },
          ]}
        />
      </div>
    </AppShell>
  );
}
