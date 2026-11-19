"use client";

import { useTranslations } from "next-intl";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
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
  // Counts so each row says how much is behind it (operator pictures
  // 18.11.2026: two bare rows read as an empty page). One note is enough to
  // read the total; the skills list is the Skills page's own query.
  const { data: notes } = useQuery({
    queryKey: ["vault", "notes", "count"],
    queryFn: () => api.vault.list({ limit: 1 }),
    staleTime: 60_000,
  });
  const { data: skills } = useQuery({
    queryKey: ["skills"],
    queryFn: () => api.skills.list() as unknown as Promise<{ skills?: unknown[]; total?: number }>,
    staleTime: 60_000,
  });
  const notesCount = typeof notes?.count === "number" ? t("notesCount", { count: notes.count }) : null;
  const skillsTotal = skills ? skills.total ?? skills.skills?.length : undefined;
  const skillsCount = typeof skillsTotal === "number" ? t("skillsCount", { count: skillsTotal }) : null;
  return (
    <AppShell mobileTitle={t("title")}>
      <div className="max-w-2xl mx-auto w-full flex flex-col gap-6" data-region="knowledge">
        <header>
          {/* Phone: the title stands in the app bar (like the chat header). */}
          <h1 className="hidden md:block display text-2xl font-semibold" style={{ color: C.textPrimary }}>
            {t("title")}
          </h1>
          <p className="text-sm md:mt-1" style={{ color: C.textSecondary }}>
            {t("lead")}
          </p>
        </header>
        <LinkList
          testId="knowledge-links"
          items={[
            { href: "/memory", icon: Brain, title: t("memoryTitle"), hint: t("memoryHint"), count: notesCount },
            { href: "/skills", icon: Puzzle, title: t("skillsTitle"), hint: t("skillsHint"), count: skillsCount },
          ]}
        />
      </div>
    </AppShell>
  );
}
