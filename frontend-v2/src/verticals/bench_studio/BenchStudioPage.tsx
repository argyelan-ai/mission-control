"use client";

import { useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import AppShell from "@/components/layout/AppShell";
import { C } from "@/lib/colors";
import { ChallengesTab } from "./ChallengesTab";
import { PromptLibraryTab } from "./PromptLibraryTab";
import type { PromptTemplate } from "./types";
import { TestSheetHost } from "./w1/sheet/TestSheetHost";

type Tab = "challenges" | "library";

export default function BenchStudioPage() {
  const t = useTranslations("bench.page");
  const [tab, setTab] = useState<Tab>("challenges");
  // "Challenge starten" from the library prefills the new-challenge dialog:
  const [prefillTemplate, setPrefillTemplate] = useState<PromptTemplate | null>(null);
  // The open challenge lives in the URL (`?challenge=<id>`): reload, back
  // button and a shared link land on the same detail page (t2 SOLL-4).
  // router.replace, not window.history — Next keeps its own copy of the URL.
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const selectedId = searchParams.get("challenge");

  function selectChallenge(id: string | null) {
    const params = new URLSearchParams(searchParams.toString());
    if (id) params.set("challenge", id);
    else params.delete("challenge");
    const query = params.toString();
    router.replace(query ? `${pathname}?${query}` : pathname, { scroll: false });
  }

  // Any tab click leaves an open challenge — also "Challenges" itself, so a
  // tap on the tab always lands on the list (and out of a dead link).
  function showTab(next: Tab) {
    setTab(next);
    if (selectedId) selectChallenge(null);
  }

  function startFromTemplate(tpl: PromptTemplate) {
    setPrefillTemplate(tpl);
    showTab("challenges");
  }

  // An open challenge is its own page: it starts with its context row and
  // title, not under the list's kicker, page title and tabs (r2 S3, K12).
  const detailOpen = tab === "challenges" && Boolean(selectedId);

  return (
    <AppShell>
      <div className="flex flex-col gap-6">
        {!detailOpen && (
          <>
            <div>
              <div className="label-sys mb-2">{t("kicker")}</div>
              <h1
                className="display text-2xl font-semibold"
                style={{ color: C.textPrimary }}
              >
                {t("title")}
              </h1>
              <p className="text-[13px] mt-1" style={{ color: C.textSecondary }}>
                {t("subtitle")}
              </p>
            </div>

            {/* Tabs — flat underline style, teal accent (Leitstand) */}
            <div
              className="flex gap-6"
              style={{ borderBottom: `1px solid ${C.border}` }}
              role="tablist"
            >
              {(
                [
                  ["challenges", t("tabChallenges")],
                  ["library", t("tabLibrary")],
                ] as [Tab, string][]
              ).map(([key, label]) => (
                <button
                  key={key}
                  role="tab"
                  aria-selected={tab === key}
                  onClick={() => showTab(key)}
                  className="min-h-11 text-sm font-medium -mb-px"
                  style={{
                    color: tab === key ? C.textPrimary : C.textSecondary,
                    borderBottom:
                      tab === key ? `2px solid ${C.accent}` : "2px solid transparent",
                  }}
                >
                  {label}
                </button>
              ))}
            </div>
          </>
        )}

        {tab === "challenges" ? (
          <ChallengesTab
            prefillTemplate={prefillTemplate}
            onPrefillConsumed={() => setPrefillTemplate(null)}
            selectedId={selectedId}
            onSelectedChange={selectChallenge}
          />
        ) : (
          <PromptLibraryTab onStartChallenge={startFromTemplate} />
        )}
      </div>
      {/* Bench W1 test sheet (package B) — opens from `/bench?test=1&…`
          (lib/benchLinks), whatever tab is showing. */}
      <TestSheetHost />
    </AppShell>
  );
}
