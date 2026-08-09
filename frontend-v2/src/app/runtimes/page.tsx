"use client";

import { useState } from "react";
import { Plus } from "lucide-react";
import AppShell from "@/components/layout/AppShell";
import { C } from "@/lib/colors";
import { AddRuntimeModal } from "./AddRuntimeModal";
import { OverviewTab } from "./OverviewTab";
import { ModelsTab } from "./ModelsTab";
import { AdminTab } from "./AdminTab";
import { useDownloadCount } from "./useDownloadCount";

const TABS = [
  { key: "overview", label: "Overview" },
  { key: "models", label: "Models" },
  { key: "admin", label: "Administration" },
] as const;
type TabKey = (typeof TABS)[number]["key"];

export default function RuntimesPage() {
  const [tab, setTab] = useState<TabKey>("overview");
  const [addOpen, setAddOpen] = useState(false);
  const downloads = useDownloadCount();

  return (
    <AppShell>
      <div className="p-6 max-w-4xl mx-auto">
        {/* Header */}
        <div className="flex items-center justify-between mb-6">
          <div>
            <div className="label-sys mb-2">System · Runtimes</div>
            <h1
              className="display text-xl font-semibold"
              style={{ color: C.textPrimary }}
            >
              Runtimes
            </h1>
            <p
              className="text-[13px] mt-0.5"
              style={{ color: C.textSecondary }}
            >
              AI model runtimes and their hosts
            </p>
          </div>

          <div className="flex items-center gap-2">
            <button
              onClick={() => setAddOpen(true)}
              className="flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-lg transition-all cursor-pointer"
              style={{
                color: C.accent,
                border: `1px solid ${C.borderAccent}`,
                background: C.accentSubtle,
              }}
            >
              <Plus size={11} />
              Add runtime
            </button>
          </div>
        </div>

        {/* Tab bar */}
        <div
          className="flex items-center gap-1 p-1 rounded-lg mb-6 w-fit"
          style={{ background: C.borderSubtle }}
        >
          {TABS.map((t) => {
            const active = tab === t.key;
            return (
              <button
                key={t.key}
                onClick={() => setTab(t.key)}
                className="flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-md transition-all cursor-pointer"
                style={{
                  background: active ? C.borderActive : "transparent",
                  color: active ? C.textPrimary : C.textMuted,
                }}
              >
                {t.label}
                {t.key === "models" && downloads > 0 && (
                  <span
                    className="flex items-center gap-1 text-[10px]"
                    style={{ color: C.warning }}
                  >
                    <span
                      className="w-1.5 h-1.5 rounded-full"
                      style={{ background: C.warning }}
                    />
                    {downloads}
                  </span>
                )}
              </button>
            );
          })}
        </div>

        {tab === "overview" && <OverviewTab />}
        {tab === "models" && <ModelsTab />}
        {tab === "admin" && <AdminTab />}
      </div>
      <AddRuntimeModal open={addOpen} onClose={() => setAddOpen(false)} />
    </AppShell>
  );
}
