// frontend-v2/src/components/shared/BrowserSessionPanel.tsx
"use client";

import { useLocale, useTranslations } from "next-intl";
import { Filter, KeyRound, Loader2, Maximize2, MonitorOff } from "lucide-react";
import { C } from "@/lib/colors";
import { StatusDot } from "@/components/shared/StatusDot";
import { OverflowMenu } from "@/components/shared/OverflowMenu";
import { BrowserTabStrip, type BrowserStripTab } from "@/components/shared/BrowserTabStrip";

/** The four states of a browser session as the panel shows them (ADR-088). */
export type BrowserSessionState = "live" | "starting" | "ended" | "none";

export interface BrowserSessionPanelProps {
  state: BrowserSessionState;
  agentName: string;
  tabs: BrowserStripTab[];
  shownId: string | null;
  /** The tab the agent is working in right now (pulse). */
  agentTabId: string | null;
  following: boolean;
  /** Live frame or, when ended, the session's last image. */
  frameSrc: string | null;
  /** When the last image was taken (ended sessions). */
  lastFrameAt?: Date;
  onSelect: (id: string) => void;
  onToggleFollow: () => void;
  onFullscreen: () => void;
  onToggleScope: () => void;
  showAllTabs: boolean;
}

/**
 * Presentational browser panel with the tab strip (ADR-088 step "tab strip
 * and states"). Data comes from the live socket in BrowserLiveView; this
 * component only decides what each session state looks like.
 *
 * Layout, top to bottom: one status row (state on the left, Follow as the
 * one primary action and ⋯ on the right — DESIGN.md K4/K11), the tab strip,
 * the page address (mono: a machine value, K7), then the picture.
 */
export function BrowserSessionPanel(props: BrowserSessionPanelProps) {
  const t = useTranslations("browserLive");
  const locale = useLocale();
  const { state, agentName, tabs, shownId, agentTabId, following, frameSrc, lastFrameAt } = props;
  const shown = tabs.find((tab) => tab.id === shownId) ?? null;

  const menu = (
    <OverflowMenu
      label={t("more")}
      actions={[
        {
          id: "scope",
          label: props.showAllTabs ? t("onlyAgent", { name: agentName }) : t("showAllTabs"),
          icon: Filter,
          onClick: props.onToggleScope,
          disabled: state !== "live",
        },
        {
          id: "fullscreen",
          label: t("fullscreen"),
          icon: Maximize2,
          onClick: props.onFullscreen,
          disabled: !frameSrc,
        },
        {
          id: "logins",
          label: t("deleteLogins"),
          icon: KeyRound,
          onClick: () => {},
          disabled: true,
          keepWhenDisabled: true,
          hint: t("deleteLoginsSoon"),
        },
      ]}
    />
  );

  if (state === "none") {
    return (
      <div className="flex flex-col h-full" data-region="browser-panel">
        <div className="flex items-center justify-end px-3 py-1 shrink-0">{menu}</div>
        <div className="flex-1 flex flex-col items-center justify-center gap-2 px-6 text-center">
          <MonitorOff size={28} style={{ color: C.textMuted }} />
          <p className="text-sm font-medium" style={{ color: C.textPrimary }}>
            {t("noneTitle")}
          </p>
          <p className="text-xs max-w-xs" style={{ color: C.textMuted }}>
            {t("noneHint", { name: agentName })}
          </p>
        </div>
      </div>
    );
  }

  const statusRow = (
    <div className="flex items-center gap-2 px-3 py-1 shrink-0" data-region="browser-status">
      <span className="flex items-center gap-2 text-xs" style={{ color: C.textSecondary }}>
        <StatusDot
          status={state === "live" ? "online" : state === "starting" ? "busy" : "offline"}
          size="sm"
          pulse={state === "starting"}
        />
        {state === "live" ? t("live") : state === "starting" ? t("starting") : t("ended")}
      </span>
      <div className="ml-auto flex items-center gap-1">
        {state === "live" && (
          <button
            type="button"
            onClick={props.onToggleFollow}
            aria-pressed={following}
            className="min-h-11 min-w-11 flex items-center justify-center gap-2 text-xs px-3 rounded-md font-medium transition-colors"
            style={
              following
                ? { background: C.accent, color: C.onAccent, border: `1px solid ${C.accent}` }
                : { border: `1px solid ${C.border}`, color: C.textSecondary }
            }
          >
            {t("follow")}
          </button>
        )}
        {menu}
      </div>
    </div>
  );

  return (
    <div className="flex flex-col h-full" data-region="browser-panel">
      {statusRow}
      {state === "live" && (
        <BrowserTabStrip
          tabs={tabs}
          shownId={shownId}
          agentTabId={agentTabId}
          agentName={agentName}
          onSelect={props.onSelect}
        />
      )}
      {state === "live" && shown?.url && (
        <div
          className="px-3 py-1 text-xs font-mono truncate border-y shrink-0"
          style={{ color: C.textMuted, borderColor: C.border }}
        >
          {shown.url}
        </div>
      )}
      <div
        className={`relative flex-1 min-h-0 flex flex-col overflow-hidden ${state === "starting" ? "items-center justify-center" : ""}`}
        style={{ background: "var(--color-bg-base)" }}
      >
        {state === "starting" && (
          <div className="flex flex-col items-center gap-2 px-6 text-center">
            <Loader2 size={20} className="animate-spin" style={{ color: C.textMuted }} />
            <p className="text-sm font-medium" style={{ color: C.textPrimary }}>
              {t("startingTitle")}
            </p>
            <p className="text-xs max-w-xs" style={{ color: C.textMuted }}>
              {t("startingHint", { name: agentName })}
            </p>
          </div>
        )}
        {state !== "starting" && frameSrc && (
          // The picture sits right under the address, not lost in the middle
          // of a tall phone screen; its own aspect ratio, never cropped.
          // eslint-disable-next-line @next/next/no-img-element -- data:/fixture image, not a Next-optimizable asset
          <img
            src={frameSrc}
            alt={state === "ended" ? t("lastFrameAlt") : t("liveViewAlt")}
            className="w-full h-auto max-h-full object-contain object-top"
          />
        )}
        {state === "ended" && lastFrameAt && (
          // A line under the picture, not a box over it (DESIGN.md K8).
          <p className="px-3 py-2 text-xs" style={{ color: C.textSecondary }}>
            {t("lastFrameFrom", {
              time: lastFrameAt.toLocaleTimeString(locale, { hour: "2-digit", minute: "2-digit", hourCycle: "h23" }),
            })}
          </p>
        )}
      </div>
    </div>
  );
}
