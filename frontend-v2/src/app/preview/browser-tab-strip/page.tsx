"use client";

// Design preview of the browser panel's tab strip and session states
// (ADR-088 step "tab strip and states"). Fixture data only — no socket, no
// login: it exists so the operator can judge the phone pictures before the
// real panel changes (DESIGN.md K14). Not linked from the app.
//   /preview/browser-tab-strip?state=live|starting|ended|none
import { Suspense, useState } from "react";
import { useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { X } from "lucide-react";
import { C } from "@/lib/colors";
import { BrowserSessionPanel, type BrowserSessionState } from "@/components/shared/BrowserSessionPanel";

const TABS = [
  { id: "t1", title: "Release notes 4.2", url: "https://docs.example.org/releases/4.2" },
  { id: "t2", title: "Pull request #128 — checkout fix", url: "https://git.example.org/shop/pull/128" },
  { id: "t3", title: "Staging · checkout", url: "https://staging.example.org/checkout" },
];

function Preview() {
  const params = useSearchParams();
  const state = (params.get("state") ?? "live") as BrowserSessionState;
  const tSessions = useTranslations("sessions");
  const [shownId, setShownId] = useState<string | null>("t3");
  const [following, setFollowing] = useState(true);
  const [showAllTabs, setShowAllTabs] = useState(false);
  const frame = state === "ended" ? "/preview/browser-frame-ended.jpg" : "/preview/browser-frame-live.jpg";
  return (
    <div className="flex flex-col h-[100dvh]" style={{ background: C.bgSurface }}>
      {/* The phone sheet's own header, as on the sessions screen. */}
      <div className="flex items-center justify-between px-4 py-3 border-b shrink-0" style={{ borderColor: C.border }}>
        <span className="text-sm font-semibold" style={{ color: C.textPrimary }}>
          {tSessions("panels.browser")}
        </span>
        <span className="flex items-center justify-center w-11 h-11" style={{ color: C.textMuted }}>
          <X size={17} />
        </span>
      </div>
      <div className="flex-1 min-h-0 flex flex-col pt-1">
        <BrowserSessionPanel
          state={state}
          agentName="Head 3f2a"
          tabs={state === "live" ? TABS : []}
          shownId={shownId}
          agentTabId="t3"
          following={following}
          frameSrc={state === "live" || state === "ended" ? frame : null}
          lastFrameAt={state === "ended" ? new Date(2026, 9, 7, 14, 32) : undefined}
          onSelect={(id) => {
            setShownId(id);
            setFollowing(false);
          }}
          onToggleFollow={() => setFollowing((f) => !f)}
          onFullscreen={() => {}}
          onToggleScope={() => setShowAllTabs((v) => !v)}
          showAllTabs={showAllTabs}
        />
      </div>
    </div>
  );
}

export default function BrowserTabStripPreviewPage() {
  return (
    <Suspense>
      <Preview />
    </Suspense>
  );
}
