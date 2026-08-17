"use client";

/**
 * ChatView — Task B6 (revised). Composes the chat timeline over an agent's
 * Claude Code transcript: header (name, live/beendet badge, detail-level
 * toggle, Chat/Terminal center-view toggle), a scroll-locked event list, the
 * approval card (only while a permission prompt is open), the truthful
 * status line, and the composer.
 *
 * Terminal used to be a side panel (PanelRail); it's now a CENTER-VIEW
 * toggle right next to the detail-level switcher — selecting it swaps the
 * whole body (timeline+composer) for `TerminalPanel` full-size instead of
 * splitting the screen. No-transcript agents (Hermes/Jarvis, or a runtime
 * 404 despite `hasTranscript` saying otherwise) force terminal mode: there's
 * no chat to show, so the "Chat" segment is disabled rather than presenting
 * a dead-end empty-state screen.
 *
 * Owns nothing about the Diff/Browser side panel — that's PanelRail's job,
 * orthogonal to this toggle.
 */
import { useEffect, useRef, useState } from "react";
import { C } from "@/lib/colors";
import { api } from "@/lib/api";
import { notify } from "@/lib/notify";
import { useChatStream } from "@/hooks/useChatStream";
import { isNoTranscriptError } from "@/lib/chatTypes";
import type { StateEvent, TimelineChatEvent } from "@/lib/chatTypes";
import { ChatMessage } from "./ChatMessage";
import { ToolRow } from "./ToolRow";
import { ThinkingRow } from "./ThinkingRow";
import { SubagentGroup } from "./SubagentGroup";
import { ToolGroup, type ActivityEvent } from "./ToolGroup";
import { ApprovalCard } from "./ApprovalCard";
import { StatusLine } from "./StatusLine";
import { Composer } from "./Composer";
import { TerminalPanel, type AgentWithState } from "./TerminalPanel";

export type DetailLevel = "compact" | "normal" | "verbose";

export const DETAIL_LEVELS: { key: DetailLevel; label: string }[] = [
  { key: "compact", label: "Kompakt" },
  { key: "normal", label: "Normal" },
  { key: "verbose", label: "Ausführlich" },
];

export type CenterView = "chat" | "terminal";

export const CENTER_VIEWS: { key: CenterView; label: string }[] = [
  { key: "chat", label: "Chat" },
  { key: "terminal", label: "Terminal" },
];

// Distance (px) from the bottom of the scroll container within which the
// view still counts as "at the bottom" — classic chat scroll-lock.
const SCROLL_LOCK_THRESHOLD_PX = 48;

function isSidechain(ev: TimelineChatEvent): boolean {
  // CommandEvent carries no `sidechain` field (chatTypes.ts) — narrow safely
  // instead of assuming every union member has the property.
  return "sidechain" in ev && ev.sidechain === true;
}

function isVisibleAtLevel(ev: TimelineChatEvent, level: DetailLevel): boolean {
  if (level !== "compact") return true;
  // Kompakt: tool/thinking rows are noise for a quick skim — hide entirely
  // rather than collapse (that's what Normal is for). Since no tool/thinking
  // event survives this filter, `Kompakt` also produces no activity groups —
  // the level's semantics are unchanged by the grouping layer.
  return ev.kind === "message" || ev.kind === "command";
}

function isActivity(ev: TimelineChatEvent): ev is ActivityEvent {
  return ev.kind === "tool" || ev.kind === "thinking";
}

export type TimelineItem =
  /** A message or command, or a run too short to be worth collapsing. */
  | { kind: "single"; event: TimelineChatEvent }
  /** A run of consecutive tool/thinking events → one ToolGroup chip. */
  | { kind: "activity"; events: ActivityEvent[] }
  /** A run of consecutive sidechain (subagent) events → one SubagentGroup. */
  | { kind: "sidechain"; events: TimelineChatEvent[] };

/** Runs shorter than this render as plain rows: collapsing a single tool call
 *  behind "1 Befehl ausgeführt" would hide its title (the useful part) and
 *  cost a tap to get it back. Two or more is where the wall starts. */
export const ACTIVITY_GROUP_MIN_SIZE = 2;

/**
 * Turns the flat event list into the timeline's render items.
 *
 * Two independent runs are accumulated: sidechain events (subagent turns,
 * unchanged behavior) and top-level tool/thinking events (the new activity
 * groups). Any other event — an assistant text message, a user message, a
 * slash command — closes both runs, which is exactly the group boundary the
 * reference contract asks for: a group covers one working stretch between two
 * things a human said or read.
 */
export function buildTimelineItems(events: TimelineChatEvent[]): TimelineItem[] {
  const out: TimelineItem[] = [];
  let sidechainRun: TimelineChatEvent[] = [];
  let activityRun: ActivityEvent[] = [];

  function flushSidechain() {
    if (sidechainRun.length > 0) {
      out.push({ kind: "sidechain", events: sidechainRun });
      sidechainRun = [];
    }
  }

  function flushActivity() {
    if (activityRun.length === 0) return;
    if (activityRun.length >= ACTIVITY_GROUP_MIN_SIZE) {
      out.push({ kind: "activity", events: activityRun });
    } else {
      for (const ev of activityRun) out.push({ kind: "single", event: ev });
    }
    activityRun = [];
  }

  for (const ev of events) {
    if (isSidechain(ev)) {
      flushActivity();
      sidechainRun.push(ev);
      continue;
    }
    flushSidechain();
    if (isActivity(ev)) {
      activityRun.push(ev);
      continue;
    }
    flushActivity();
    out.push({ kind: "single", event: ev });
  }

  flushActivity();
  flushSidechain();
  return out;
}

function renderTimelineEvent(ev: TimelineChatEvent, detailLevel: DetailLevel) {
  switch (ev.kind) {
    case "message":
      return <ChatMessage key={ev.uuid} ev={ev} />;
    case "tool":
      // toolUseId ?? uuid: parallel tool calls in one assistant turn share
      // the turn's uuid but carry distinct toolUseIds (useChatStream.ts).
      return <ToolRow key={ev.toolUseId ?? ev.uuid} ev={ev} detailLevel={detailLevel} />;
    case "thinking":
      return <ThinkingRow key={ev.uuid} ev={ev} detailLevel={detailLevel} />;
    case "command":
      return (
        <div key={ev.uuid} className="w-full px-4 py-1.5 text-xs font-mono" style={{ color: C.textMuted }}>
          {ev.command}
        </div>
      );
    default:
      return null;
  }
}

interface ChatViewProps {
  agent: AgentWithState | null;
  /** Sidebar-derived, mirrors the backend's fail-closed transcript gate
   *  (resolve_transcript_dir). `false` skips the history/SSE fetch outright
   *  and forces terminal mode — there's nothing to chat with. */
  hasTranscript: boolean;
  detailLevel: DetailLevel;
  onDetailLevelChange: (level: DetailLevel) => void;
  centerView: CenterView;
  onCenterViewChange: (view: CenterView) => void;
  /** Bumped by the parent page's `useTerminalRemountSignal` (backend-driven
   *  runtime switch) to force-remount just the embedded TerminalPanel. */
  terminalRemountTick?: number;
  /** Fires whenever the chat stream's `state.status` changes (including to
   *  `null` while there's no stream yet). Lets the parent page derive a
   *  plain boolean like DiffPanel's `refreshHot` (status === "working")
   *  without duplicating `useChatStream`'s SSE connection just to read one
   *  field — ChatView already owns the one subscription for this agent. */
  onStatusChange?: (status: StateEvent["status"] | null) => void;
}

export function ChatView({
  agent,
  hasTranscript,
  detailLevel,
  onDetailLevelChange,
  centerView,
  onCenterViewChange,
  terminalRemountTick = 0,
  onStatusChange,
}: ChatViewProps) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const [stickToBottom, setStickToBottom] = useState(true);

  const streamEnabled = hasTranscript && !!agent;
  const stream = useChatStream(agent?.id ?? null, streamEnabled);

  const streamStatus = stream.state?.status ?? null;
  useEffect(() => {
    onStatusChange?.(streamStatus);
  }, [streamStatus, onStatusChange]);

  // Belt and braces: even if the sidebar thought this agent had a
  // transcript, a runtime 404 from the history fetch forces terminal mode
  // the same way a known-no-transcript agent (Hermes/Jarvis) does — no
  // separate dead-end empty-state screen, the toggle just can't reach "chat".
  const canChat = hasTranscript && !isNoTranscriptError(stream.error);
  const effectiveView: CenterView = canChat ? centerView : "terminal";

  useEffect(() => {
    if (!stickToBottom) return;
    const el = scrollRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [stream.events, stickToBottom]);

  function handleScroll() {
    const el = scrollRef.current;
    if (!el) return;
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    setStickToBottom(distanceFromBottom < SCROLL_LOCK_THRESHOLD_PX);
  }

  function handleSend(text: string) {
    if (!agent) return;
    api.chat.sendText(agent.id, text).catch(() => notify.error("Senden fehlgeschlagen"));
  }

  function handleStop() {
    if (!agent) return;
    api.chat.sendKeys(agent.id, ["Escape"]).catch(() => notify.error("Stop fehlgeschlagen"));
  }

  function handleAnswer(key: string) {
    if (!agent) return;
    // Digit alone, no trailing Enter — numbered pickers accept the bare key.
    api.chat.sendKeys(agent.id, [key]).catch(() => notify.error("Antwort fehlgeschlagen"));
  }

  if (!agent) {
    return (
      <div className="flex flex-1 items-center justify-center text-[13px]" style={{ color: C.textMuted }}>
        Wähle eine Session in der Seitenleiste.
      </div>
    );
  }

  const visibleEvents = stream.events.filter((ev) => isVisibleAtLevel(ev, detailLevel));
  const items = buildTimelineItems(visibleEvents);
  const prompt = stream.state?.status === "permission_prompt" ? stream.state.prompt : null;

  return (
    <div className="flex flex-col flex-1 min-h-0 overflow-hidden">
      <div className="flex items-center gap-2 px-4 py-2.5 border-b shrink-0" style={{ borderColor: C.border }}>
        <span className="text-[13px] font-medium truncate" style={{ color: C.textPrimary }}>
          {agent.name}
        </span>
        {canChat && stream.session && (
          <span
            className="text-[10px] font-medium px-1.5 py-0.5 rounded font-mono shrink-0"
            style={{
              background: stream.session.live ? `${C.online}1A` : C.bgHover,
              color: stream.session.live ? C.online : C.textMuted,
              border: `1px solid ${stream.session.live ? `${C.online}33` : C.border}`,
            }}
          >
            {stream.session.live ? "live" : "beendet"}
          </span>
        )}

        <div className="ml-auto flex items-center gap-2 shrink-0">
          {effectiveView === "chat" && (
            <div
              className="flex items-center rounded-md overflow-hidden"
              style={{ border: `1px solid ${C.border}` }}
            >
              {DETAIL_LEVELS.map(({ key, label }) => (
                <button
                  key={key}
                  type="button"
                  onClick={() => onDetailLevelChange(key)}
                  aria-pressed={detailLevel === key}
                  className="px-2.5 py-1.5 text-[10px] font-medium transition-colors cursor-pointer whitespace-nowrap"
                  style={{
                    background: detailLevel === key ? C.accentSubtle : "transparent",
                    color: detailLevel === key ? C.accent : C.textMuted,
                    borderRight: key !== "verbose" ? `1px solid ${C.border}` : undefined,
                  }}
                >
                  {label}
                </button>
              ))}
            </div>
          )}

          <div
            className="flex items-center rounded-md overflow-hidden"
            style={{ border: `1px solid ${C.border}` }}
          >
            {CENTER_VIEWS.map(({ key, label }) => {
              const disabled = key === "chat" && !canChat;
              return (
                <button
                  key={key}
                  type="button"
                  disabled={disabled}
                  onClick={() => onCenterViewChange(key)}
                  aria-pressed={effectiveView === key}
                  title={disabled ? "Kein Transkript verfügbar" : undefined}
                  className="px-2.5 py-1.5 text-[10px] font-medium transition-colors cursor-pointer disabled:cursor-not-allowed disabled:opacity-40 whitespace-nowrap"
                  style={{
                    background: effectiveView === key ? C.accentSubtle : "transparent",
                    color: effectiveView === key ? C.accent : C.textMuted,
                    borderRight: key !== "terminal" ? `1px solid ${C.border}` : undefined,
                  }}
                >
                  {label}
                </button>
              );
            })}
          </div>
        </div>
      </div>

      {effectiveView === "terminal" ? (
        <TerminalPanel key={`term-${terminalRemountTick}`} agent={agent} />
      ) : (
        <>
          <div ref={scrollRef} onScroll={handleScroll} className="flex-1 min-h-0 overflow-y-auto flex flex-col">
            {items.length === 0 ? (
              <div className="flex flex-1 items-center justify-center text-[13px]" style={{ color: C.textMuted }}>
                {stream.loading ? "Lädt…" : "Noch keine Nachrichten."}
              </div>
            ) : (
              items.map((item) => {
                if (item.kind === "sidechain") {
                  return <SubagentGroup key={`sidechain-${item.events[0].uuid}`} events={item.events} />;
                }
                if (item.kind === "activity") {
                  return (
                    <ToolGroup
                      key={`activity-${item.events[0].uuid}`}
                      events={item.events}
                      detailLevel={detailLevel}
                    />
                  );
                }
                return renderTimelineEvent(item.event, detailLevel);
              })
            )}
          </div>

          {prompt && (
            <div className="px-3 pt-2">
              <ApprovalCard
                prompt={prompt}
                onAnswer={handleAnswer}
                onShowTerminal={() => onCenterViewChange("terminal")}
              />
            </div>
          )}

          <StatusLine state={stream.state} connected={stream.connected} />
          <Composer agentId={agent.id} usage={stream.usage} state={stream.state} onSend={handleSend} onStop={handleStop} sessionLive={stream.session?.live ?? false} />
        </>
      )}
    </div>
  );
}
