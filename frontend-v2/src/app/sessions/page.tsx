"use client";

import { useEffect, useState, Suspense } from "react";
import { useSearchParams } from "next/navigation";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { X } from "lucide-react";
import { api } from "@/lib/api";
import { useAppStore } from "@/lib/store";
import { C } from "@/lib/colors";
import { BrowserLiveView } from "@/components/shared/BrowserLiveView";
import { SessionSidebar } from "@/components/chat/SessionSidebar";
import { ChatView, type CenterView, type DetailLevel, CENTER_VIEWS, DETAIL_LEVELS } from "@/components/chat/ChatView";
import { DiffPanel } from "@/components/chat/DiffPanel";
import { PanelRail, type PanelKind } from "@/components/chat/PanelRail";
import { agentIsRunning, type AgentWithState } from "@/components/chat/TerminalPanel";
import type { StateEvent } from "@/lib/chatTypes";
import AppShell from "@/components/layout/AppShell";
import { notify } from "@/lib/notify";
import { useTerminalRemountSignal } from "@/hooks/useTerminalRemountSignal";

// ── Last-selected-agent persistence ─────────────────────────────────────────
// Same try/catch-wrapped localStorage pattern as runtimes/page.tsx's
// CTX_STORAGE_KEY. ?agent=<id> (from the Agents list "open session" button)
// takes precedence over the stored value — see the restore effect below.
const LAST_AGENT_STORAGE_KEY = "mc-sessions-last-agent";

function loadLastAgentId(): string | null {
  try {
    return localStorage.getItem(LAST_AGENT_STORAGE_KEY);
  } catch { return null; }
}

function saveLastAgentId(id: string) {
  try {
    localStorage.setItem(LAST_AGENT_STORAGE_KEY, id);
  } catch {}
}

// ── Panel + detail-level + center-view persistence ──────────────────────────
const PANEL_STORAGE_KEY = "mc.chat.panel";
const DETAIL_STORAGE_KEY = "mc.chat.detail";
const VIEW_STORAGE_KEY = "mc.chat.view";
// Diff + Browser only — Terminal moved from the side panel to ChatView's own
// center-view toggle (mc.chat.view). A stale "terminal" value from before
// that change simply falls back to `null` here (not in this allow-list).
const VALID_PANELS: PanelKind[] = ["diff", "browser"];
const VALID_DETAIL_LEVELS = DETAIL_LEVELS.map((d) => d.key);
const VALID_CENTER_VIEWS = CENTER_VIEWS.map((v) => v.key);

function loadActivePanel(): PanelKind | null {
  try {
    const v = localStorage.getItem(PANEL_STORAGE_KEY);
    return (VALID_PANELS as string[]).includes(v ?? "") ? (v as PanelKind) : null;
  } catch { return null; }
}

function saveActivePanel(panel: PanelKind | null) {
  try {
    if (panel) localStorage.setItem(PANEL_STORAGE_KEY, panel);
    else localStorage.removeItem(PANEL_STORAGE_KEY);
  } catch {}
}

function loadDetailLevel(): DetailLevel {
  try {
    const v = localStorage.getItem(DETAIL_STORAGE_KEY);
    return (VALID_DETAIL_LEVELS as string[]).includes(v ?? "") ? (v as DetailLevel) : "normal";
  } catch { return "normal"; }
}

function saveDetailLevel(level: DetailLevel) {
  try {
    localStorage.setItem(DETAIL_STORAGE_KEY, level);
  } catch {}
}

function loadCenterView(): CenterView {
  try {
    const v = localStorage.getItem(VIEW_STORAGE_KEY);
    return (VALID_CENTER_VIEWS as string[]).includes(v ?? "") ? (v as CenterView) : "chat";
  } catch { return "chat"; }
}

function saveCenterView(view: CenterView) {
  try {
    localStorage.setItem(VIEW_STORAGE_KEY, view);
  } catch {}
}

// ── Sidebar collapse persistence ────────────────────────────────────────────
// Desktop rail only (SessionSidebar's `collapsed` prop is a no-op on the
// mobile `sheet` variant, which has its own collapsed-by-default toggle).
const SIDEBAR_STORAGE_KEY = "mc.chat.sidebar";

function loadSidebarCollapsed(): boolean {
  try {
    return localStorage.getItem(SIDEBAR_STORAGE_KEY) === "collapsed";
  } catch { return false; }
}

function saveSidebarCollapsed(collapsed: boolean) {
  try {
    localStorage.setItem(SIDEBAR_STORAGE_KEY, collapsed ? "collapsed" : "open");
  } catch {}
}

// ── Transcript availability ─────────────────────────────────────────────────
// Mirrors the backend's fail-closed gate exactly (resolve_transcript_dir,
// backend/app/services/transcript_chat.py): cli-bridge agents always have a
// transcript; host agents only if their slug is the Boss (whose transcript
// lives in Mark's own ~/.claude, privacy-filtered separately). Every other
// host agent (Hermes, Jarvis) and "manual"/"claude-code" runtime agents have
// none — chat falls back to the empty state + terminal shortcut.
const BOSS_SLUGS = new Set(["boss", "boss-host"]);

function agentHasTranscript(agent: AgentWithState | null | undefined): boolean {
  if (!agent) return false;
  if (agent.agent_runtime === "cli-bridge") return true;
  return agent.agent_runtime === "host" && !!agent.slug && BOSS_SLUGS.has(agent.slug);
}

// ── Main Page ─────────────────────────────────────────────────────────────────

function SessionsPageContent() {
  const t = useTranslations("sessions");
  const qc = useQueryClient();
  const searchParams = useSearchParams();
  const { activeBoardId } = useAppStore();
  const [selected, setSelected] = useState<AgentWithState | null>(null);
  // Mobile (<md) stack navigation: which pane is visible. Desktop (≥md) ignores
  // this and always shows the split. Kept separate from `selected` so a
  // return-to-list action doesn't null `selected` (which would immediately
  // re-trigger the auto-select effect below and snap back).
  const [mobileView, setMobileView] = useState<"list" | "chat">("list");
  const [activePanel, setActivePanelState] = useState<PanelKind | null>(null);
  // Chat stream status, mirrored up from ChatView's own useChatStream
  // subscription (ChatView.onStatusChange) — drives DiffPanel's refreshHot
  // without opening a second SSE connection just to read one field.
  const [chatStatus, setChatStatus] = useState<StateEvent["status"] | null>(null);
  const [detailLevel, setDetailLevelState] = useState<DetailLevel>("normal");
  const [centerView, setCenterViewState] = useState<CenterView>("chat");
  const [sidebarCollapsed, setSidebarCollapsedState] = useState(false);
  const [restartTick, setRestartTick] = useState<Record<string, number>>({});

  // Persisted state is read from localStorage after mount (SSR has no
  // window) — same pattern as loadLastAgentId's restore effect below.
  useEffect(() => {
    setActivePanelState(loadActivePanel());
    setDetailLevelState(loadDetailLevel());
    setCenterViewState(loadCenterView());
    setSidebarCollapsedState(loadSidebarCollapsed());
  }, []);

  function setActivePanel(panel: PanelKind | null) {
    setActivePanelState(panel);
    saveActivePanel(panel);
  }

  function setDetailLevel(level: DetailLevel) {
    setDetailLevelState(level);
    saveDetailLevel(level);
  }

  function setCenterView(view: CenterView) {
    setCenterViewState(view);
    saveCenterView(view);
  }

  function setSidebarCollapsed(collapsed: boolean) {
    setSidebarCollapsedState(collapsed);
    saveSidebarCollapsed(collapsed);
  }

  const { data: dockerAgents = [], isLoading, isError } = useQuery({
    queryKey: ["agents", "docker-sessions"],
    queryFn: () => api.agents.listDockerSessions(),
    refetchInterval: 10_000,
  });

  const { data: hostAgents = [] } = useQuery({
    queryKey: ["agents", "host-sessions"],
    queryFn: () => api.agents.listHostSessions(),
    refetchInterval: 5_000,
  });

  const { data: tasks = [] } = useQuery({
    queryKey: ["tasks", activeBoardId],
    queryFn: () => api.tasks.list(activeBoardId!),
    enabled: !!activeBoardId,
  });

  const { data: projects = [] } = useQuery({
    queryKey: ["projects", activeBoardId],
    queryFn: () => api.projects.list(activeBoardId!),
    enabled: !!activeBoardId,
  });

  const agents: AgentWithState[] = [...dockerAgents, ...hostAgents];

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["agents", "docker-sessions"] });
    qc.invalidateQueries({ queryKey: ["agents", "host-sessions"] });
  };

  // Restore the last-viewed agent on load: ?agent=<id> (Agents list "open
  // session" button) wins over the localStorage memory, which wins over the
  // previous fallback (first running agent, else the first in the list).
  useEffect(() => {
    if (agents.length === 0 || selected) return;
    const paramId = searchParams.get("agent");
    const paramAgent = paramId ? agents.find((a) => a.id === paramId) : undefined;
    // A deep link names one session, so it opens it: on mobile that means
    // going straight to the chat screen. A merely *remembered* selection does
    // not — the phone opens on the list, which is the overview, and one tap
    // reaches the remembered session (still highlighted there).
    if (paramAgent) { setSelected(paramAgent); setMobileView("chat"); return; }

    const storedId = loadLastAgentId();
    const storedAgent = storedId ? agents.find((a) => a.id === storedId) : undefined;
    if (storedAgent) { setSelected(storedAgent); return; }

    const running = agents.find((a) => agentIsRunning(a));
    setSelected(running ?? agents[0]);
  }, [agents, selected, searchParams]);

  // Remember the selection for the next visit.
  useEffect(() => {
    if (selected) saveLastAgentId(selected.id);
  }, [selected]);

  // Phase 15 T3.7: re-mount the terminal when the backend switches the
  // selected agent's runtime (incl. cross-image recreate). Without this
  // the WebSocket still points at the killed container's tmux PTY and
  // shows a frozen buffer.
  useTerminalRemountSignal(selected?.id ?? null, (payload) => {
    if (!selected) return;
    setRestartTick((prev) => ({ ...prev, [selected.id]: (prev[selected.id] ?? 0) + 1 }));
    invalidate();
    notify.success(
      payload.image_changed
        ? t("runtimeSwitchedRebuilt")
        : t("runtimeSwitchedRestarted"),
    );
  });

  function handleSelect(agentId: string) {
    const agent = agents.find((a) => a.id === agentId);
    if (!agent) return;
    setSelected(agent);
    setMobileView("chat");
  }

  const panelTitle = activePanel === "diff" ? "Diff" : activePanel === "browser" ? "Browser" : "";

  // Mobile stack navigation. Desktop (≥md) ignores all three: it always shows
  // list + chat side by side, so every branch below resolves via `md:` classes.
  const onChatScreen = mobileView === "chat" && !!selected;
  const selectedTaskTitle = selected?.current_task_id
    ? tasks.find((task) => task.id === selected.current_task_id)?.title ?? null
    : null;

  return (
    <AppShell fullHeight>
      {/* Full-bleed on mobile: AppShell's <main> pads the content column so
          ordinary pages breathe (px-4, 1rem bottom, and a top inset of
          safe-area + 5.5rem against a fixed header that is only safe-area +
          4rem tall). A chat must not — phone width and height are the scarce
          resources, and the composer belongs on the floor directly above the
          app's bottom tab bar. These negative margins reclaim that padding
          below md (leaving 0.5rem above the header) and nothing above it. */}
      <div
        className="flex flex-col flex-1 overflow-hidden -mx-4 -mb-4 -mt-4 md:mx-0 md:mb-0 md:mt-0"
        style={{ background: C.bgDeep }}
      >
        {isError && (
          <div className="text-red-400 text-xs p-4">{t("backendConnectionFailed")}</div>
        )}
        {/* No page-title row. The app bar already says SESSIONS and the bottom
            tab bar already says SESS, so an icon + "Agent Terminals" + count
            repeated the answer twice and spent a whole row of vertical space
            doing it. The islands claim that space instead. */}

        {/* Split Layout: [sidebar | chat | panel rail | panel] — desktop:
            Codex-style floating islands (padded gap, rounded-xl, 1px border,
            overflow-hidden) via `md:` utilities only; mobile stays exactly
            as before (full-bleed, no gap/border/radius — "don't waste phone
            width"), same components/state, zero mobile-visible change. */}
        <div className="flex flex-col md:flex-row flex-1 min-h-0 overflow-hidden md:p-2 md:gap-2">
          {/* Mobile stack, screen 1: the session list as a real screen you tap
              into — not the dropdown sheet it used to be, which had no content
              area of its own and left the page blank whenever a restored
              selection kept `mobileView` on "list". */}
          <div
            className={`${onChatScreen ? "hidden" : "flex"} md:hidden flex-1 min-h-0 overflow-hidden`}
            data-testid="session-list-mobile"
          >
            <SessionSidebar
              agents={agents}
              tasks={tasks}
              projects={projects}
              selectedId={selected?.id ?? null}
              onSelect={handleSelect}
              variant="list"
              hasTranscript={(id) => agentHasTranscript(agents.find((a) => a.id === id))}
            />
          </div>
          {/* `hidden md:flex` already gates this to desktop-only — the island
              chrome doesn't need its own `md:` prefixes to stay mobile-inert,
              but keeps them anyway for a diff that reads consistently with
              the chat/panel islands below (which ARE visible on both). No
              background override here: SessionSidebar's rail variant already
              paints its own `bg-surface` — its self-drawn `border-right` was
              dropped instead (this wrapper's border now owns that edge). */}
          {/* ONE ground for the page and all three islands (bg-deep). The
              islands used to sit a tonal step above the page, which read as
              three panels floating on a darker backdrop — the operator saw the
              seam around the chat and asked for it gone. Separation now comes
              from the 1px border and the 8px gap alone, Codex-style, so the
              whole surface reads as one instrument. Borders are the stronger
              neutral step (0.16) because they are now the only separator. */}
          <div
            className="hidden md:flex shrink-0 md:rounded-xl md:overflow-hidden md:border md:border-[var(--color-border-strong)]"
            data-testid="sidebar-desktop"
          >
            <SessionSidebar
              agents={agents}
              tasks={tasks}
              projects={projects}
              selectedId={selected?.id ?? null}
              onSelect={handleSelect}
              variant="rail"
              hasTranscript={(id) => agentHasTranscript(agents.find((a) => a.id === id))}
              collapsed={sidebarCollapsed}
              onToggleCollapse={() => setSidebarCollapsed(!sidebarCollapsed)}
            />
          </div>

          {/* Mobile stack, screen 2 (and the desktop island): the chat itself.
              ChatView/TerminalPanel paint no background of their own, so this
              wrapper supplies bg-base — and on mobile it is the whole screen,
              edge to edge, no island chrome. */}
          <div
            className={`${onChatScreen ? "flex" : "hidden"} md:flex flex-1 min-w-0 min-h-0 overflow-hidden flex-col md:rounded-xl md:border md:border-[var(--color-border-strong)]`}
            data-testid="chat-column"
          >
            {isLoading && !selected ? null : (
              <ChatView
                key={selected?.id ?? "none"}
                agent={selected}
                hasTranscript={agentHasTranscript(selected)}
                detailLevel={detailLevel}
                onDetailLevelChange={setDetailLevel}
                centerView={centerView}
                onCenterViewChange={setCenterView}
                terminalRemountTick={selected ? restartTick[selected.id] ?? 0 : 0}
                onStatusChange={setChatStatus}
                onBack={() => setMobileView("list")}
                contextLine={selectedTaskTitle}
                onOpenPanel={setActivePanel}
              />
            )}
          </div>

          {/* Panel rail — desktop only, its own slim island next to the chat.
              On mobile the same panels are reached from the chat header's
              options sheet; the rail used to be a `fixed bottom-0` bar there,
              which covered the app's own bottom tab bar. */}
          <PanelRail active={activePanel} onSelect={setActivePanel} />

          {/* Panel content — desktop: its own island column; mobile: full-
              screen overlay with its own close button (single markup block,
              no duplicate render — Tailwind `md:` variants do the switch).
              `overflow-hidden` added so DiffPanel/BrowserLiveView content
              clips to the desktop radius instead of squaring off the
              corners; background stays unconditional (the mobile overlay
              needs an opaque backing too), only the border/radius are
              desktop-only (mobile is edge-to-edge, no island chrome). */}
          {activePanel && (
            <div
              // Mobile: a sheet filling everything below the app's own top bar
              // (see --mobile-appbar-h — this sits inside the content column's
              // z-10 stacking context, so it cannot paint over that z-40
              // header; anchoring to it beats being half-hidden behind it).
              className="fixed inset-x-0 bottom-0 top-[var(--mobile-appbar-h)] z-40 flex flex-col overflow-hidden md:static md:inset-auto md:z-auto md:w-[45%] md:max-w-[720px] md:rounded-xl md:border"
              style={{ background: C.bgDeep, borderColor: C.borderActive }}
            >
              <div
                className="flex md:hidden items-center justify-between px-4 py-3 border-b shrink-0"
                style={{ borderColor: C.border }}
              >
                <span className="text-[14px] font-semibold" style={{ color: C.textPrimary }}>
                  {panelTitle}
                </span>
                <button
                  type="button"
                  onClick={() => setActivePanel(null)}
                  aria-label="Schliessen"
                  className="flex items-center justify-center w-10 h-10 rounded-lg cursor-pointer"
                  style={{ color: C.textMuted }}
                >
                  <X size={17} />
                </button>
              </div>
              <div className="flex-1 min-h-0 flex flex-col overflow-hidden">
                {activePanel === "diff" && selected && (
                  <DiffPanel agentId={selected.id} refreshHot={chatStatus === "working"} />
                )}
                {activePanel === "browser" && <BrowserLiveView />}
              </div>
            </div>
          )}
        </div>
      </div>
    </AppShell>
  );
}

// useSearchParams requires a Suspense boundary in the app router (same
// wrapping as /tasks's ?taskId= deep link).
export default function SessionsPage() {
  return (
    <Suspense fallback={null}>
      <SessionsPageContent />
    </Suspense>
  );
}
