"use client";

import { useState } from "react";
import AppShell from "@/components/layout/AppShell";
import { C } from "@/lib/colors";
import { ChallengesTab } from "./ChallengesTab";
import { PromptLibraryTab } from "./PromptLibraryTab";
import type { PromptTemplate } from "./types";

type Tab = "challenges" | "library";

export default function BenchStudioPage() {
  const [tab, setTab] = useState<Tab>("challenges");
  // "Challenge starten" from the library prefills the new-challenge dialog:
  const [prefillTemplate, setPrefillTemplate] = useState<PromptTemplate | null>(null);

  function startFromTemplate(tpl: PromptTemplate) {
    setPrefillTemplate(tpl);
    setTab("challenges");
  }

  return (
    <AppShell>
      <div className="flex flex-col gap-6">
        <div>
          <h1
            className="text-2xl font-bold tracking-tight"
            style={{ color: C.textPrimary }}
          >
            Benchmark Studio
          </h1>
          <p className="text-sm mt-1" style={{ color: C.textSecondary }}>
            One-shot model duels — generate, render, compose, post.
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
              ["challenges", "Challenges"],
              ["library", "Prompt Library"],
            ] as [Tab, string][]
          ).map(([key, label]) => (
            <button
              key={key}
              role="tab"
              aria-selected={tab === key}
              onClick={() => setTab(key)}
              className="pb-2 text-sm font-medium -mb-px"
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

        {tab === "challenges" ? (
          <ChallengesTab
            prefillTemplate={prefillTemplate}
            onPrefillConsumed={() => setPrefillTemplate(null)}
          />
        ) : (
          <PromptLibraryTab onStartChallenge={startFromTemplate} />
        )}
      </div>
    </AppShell>
  );
}
