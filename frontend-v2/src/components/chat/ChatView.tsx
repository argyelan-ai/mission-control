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
import { ChevronLeft, MessagesSquare, MoreHorizontal } from "lucide-react";
import { C } from "@/lib/colors";
import { api } from "@/lib/api";
import { notify } from "@/lib/notify";
import { useChatStream } from "@/hooks/useChatStream";
import { isAgentStartingError, isNoTranscriptError, resolveAliveness } from "@/lib/chatTypes";
import type { StateEvent, TimelineChatEvent } from "@/lib/chatTypes";
import { ChatMessage } from "./ChatMessage";
import { ToolRow } from "./ToolRow";
import { ThinkingRow } from "./ThinkingRow";
import { SubagentGroup } from "./SubagentGroup";
import { ToolGroup, type ActivityEvent } from "./ToolGroup";
import { CommandRow } from "./CommandRow";
import { ApprovalCard } from "./ApprovalCard";
import { StatusLine } from "./StatusLine";
import { Composer } from "./Composer";
import { TerminalPanel, type AgentWithState } from "./TerminalPanel";
import { ChatOptionsSheet } from "./ChatOptionsSheet";
import { CENTER_VIEWS, DETAIL_LEVELS, type CenterView, type DetailLevel } from "./chatOptions";
import type { PanelKind } from "./PanelRail";

// Re-exported from their own module so ChatOptionsSheet can use them without
// importing ChatView back (see chatOptions.ts). Importers are unaffected.
export { CENTER_VIEWS, DETAIL_LEVELS };
export type { CenterView, DetailLevel };

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

/** Timeline items mounted in the first commit — roughly a screenful, so the
 *  operator sees the end of the conversation immediately. */
export const INITIAL_RENDER_WINDOW = 30;

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

/**
 * The uuids of assistant messages whose model differs from the previous
 * assistant message's — the only turns where naming the model tells the
 * reader something. Stamping every turn with the same name is noise; a switch
 * mid-session (this fleet runs several models) is a fact worth seeing.
 */
export function modelBadgeUuids(events: TimelineChatEvent[]): Set<string> {
  const out = new Set<string>();
  let previous: string | null = null;
  for (const ev of events) {
    if (ev.kind !== "message" || ev.role !== "assistant" || !ev.model) continue;
    if (ev.model !== previous) out.add(ev.uuid);
    previous = ev.model;
  }
  return out;
}

/** Loading placeholder shaped like what is about to arrive (a couple of
 *  paragraphs and a tool row), rather than a spinner parked mid-content. The
 *  pulse is reduced-motion aware via globals.css. */
function TimelineSkeleton() {
  const rows = [
    ["88%", "96%", "62%"],
    ["44%"],
    ["92%", "78%"],
  ];
  return (
    <div className="flex flex-col gap-5 px-4 md:px-5 pt-3 animate-pulse" data-testid="timeline-skeleton" aria-hidden="true">
      {rows.map((widths, block) => (
        <div key={block} className="flex flex-col gap-2">
          {widths.map((w, line) => (
            <div
              key={line}
              className="h-3 rounded-sm"
              style={{ width: w, background: C.bgElevated }}
            />
          ))}
        </div>
      ))}
      <span className="sr-only">Transkript wird geladen…</span>
    </div>
  );
}

function renderTimelineEvent(ev: TimelineChatEvent, detailLevel: DetailLevel, showModel = false) {
  switch (ev.kind) {
    case "message":
      return <ChatMessage key={ev.uuid} ev={ev} showModel={showModel} />;
    case "tool":
      // toolUseId ?? uuid: parallel tool calls in one assistant turn share
      // the turn's uuid but carry distinct toolUseIds (useChatStream.ts).
      return <ToolRow key={ev.toolUseId ?? ev.uuid} ev={ev} detailLevel={detailLevel} />;
    case "thinking":
      return <ThinkingRow key={ev.uuid} ev={ev} detailLevel={detailLevel} />;
    case "command":
      return <CommandRow key={ev.uuid} ev={ev} detailLevel={detailLevel} />;
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
  /** Mobile stack navigation: present = show the back chevron that returns to
   *  the session list. Omitted on desktop, where the list is always visible
   *  beside the chat and a back affordance would be a lie. */
  onBack?: () => void;
  /** One short line under the agent name on the mobile header — what this
   *  session is currently about (its task title). Desktop's header stays a
   *  single dense row, so this is <md only. */
  contextLine?: string | null;
  /** Lets the mobile options sheet open the side panels the desktop rail
   *  owns. Omitted = no Panels section in the sheet. */
  onOpenPanel?: (panel: PanelKind) => void;
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
  onBack,
  contextLine,
  onOpenPanel,
}: ChatViewProps) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const [stickToBottom, setStickToBottom] = useState(true);
  const [optionsOpen, setOptionsOpen] = useState(false);
  // Chunked first paint: a full Boss transcript is ~200 events of ReactMarkdown,
  // and rendering all of it in one commit blocked the main thread long enough
  // that the page stopped answering (measured: repeated >5s stalls, 741ms LCP
  // render delay). The operator reads the END of the conversation first, so the
  // last screenful is mounted immediately and the rest follows in the next
  // frame. No virtualization library — the list is bounded at MAX_CHAT_EVENTS
  // and this costs one boolean.
  const [renderAll, setRenderAll] = useState(false);

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

  // `renderAll` is in the deps for a reason: when the deferred remainder mounts,
  // content appears ABOVE the viewport, so a scroll position left untouched
  // would silently show older messages instead of the end.
  useEffect(() => {
    if (!stickToBottom) return;
    const el = scrollRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [stream.events, stickToBottom, renderAll]);

  // One frame later, not on a timer: the browser gets to paint the tail first,
  // which is the whole point.
  useEffect(() => {
    if (renderAll) return;
    if (typeof requestAnimationFrame === "undefined") {
      setRenderAll(true);
      return;
    }
    const handle = requestAnimationFrame(() => setRenderAll(true));
    return () => cancelAnimationFrame(handle);
  }, [renderAll]);

  // The effect above fires on new events, which is not enough: the mobile
  // stack keeps the off-screen pane mounted with `display: none`, where the
  // container measures 0 and "scroll to the bottom" is a no-op. Nothing then
  // changes when it becomes visible, so the timeline opened at the very top of
  // a 6000px history. Observing the container's own box catches that, plus
  // every later resize (window, rotation, on-screen keyboard).
  useEffect(() => {
    const el = scrollRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => {
      if (!stickToBottom) return;
      el.scrollTop = el.scrollHeight;
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, [stickToBottom]);

  function handleScroll() {
    const el = scrollRef.current;
    if (!el) return;
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    setStickToBottom(distanceFromBottom < SCROLL_LOCK_THRESHOLD_PX);
  }

  function handleSend(text: string) {
    if (!agent) return;
    // Echo BEFORE the request: the bubble and the scroll happen in this frame,
    // not a second later when the tailer has polled the transcript. The request
    // failing removes the echo again — an echo that outlived a failed send would
    // be the one thing worse than the old delay.
    stream.echoSent(text);
    deliver(text);
  }

  /** The actual delivery, separated so the `agent_starting` retry travels the
   *  exact same path — including this error handling — instead of a second,
   *  subtly different one. */
  function deliver(text: string) {
    if (!agent) return;
    api.chat.sendText(agent.id, text).catch((err) => {
      if (isAgentStartingError(err)) {
        // Not a failure: the agent is booting. The echo says so and the send is
        // retried once. No toast — nothing has gone wrong yet.
        stream.echoAgentStarting(text, () => deliver(text));
        return;
      }
      stream.echoFailed(text);
      notify.error("Senden fehlgeschlagen");
    });
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
  // Tail first; the remainder joins one frame later (see `renderAll`).
  const visibleItems = renderAll ? items : items.slice(-INITIAL_RENDER_WINDOW);
  const modelBadges = modelBadgeUuids(visibleEvents);
  // Single source for how alive this session is — see resolveAliveness for why
  // a missing server field must never be read as "ended".
  const aliveness = resolveAliveness(stream.session);
  const prompt = stream.state?.status === "permission_prompt" ? stream.state.prompt : null;

  return (
    <div className="flex flex-col flex-1 min-h-0 overflow-hidden">
      {/* One header for both breakpoints — the parts that only make sense on
          a phone (back chevron, context line, options button) carry
          `md:hidden`, the desktop toolbar carries `hidden md:flex`. Rendering
          two headers would duplicate the agent name in the DOM for no gain. */}
      <div
        className="flex items-center gap-2 pl-1 pr-2 md:px-4 py-1.5 md:py-2.5 border-b shrink-0"
        style={{ borderColor: C.border }}
      >
        {onBack && (
          <button
            type="button"
            onClick={onBack}
            aria-label="Zurück zur Sessionliste"
            className="flex md:hidden items-center justify-center w-10 h-10 shrink-0 rounded-lg cursor-pointer"
            style={{ color: C.textSecondary }}
          >
            <ChevronLeft size={20} />
          </button>
        )}

        <div className="flex-1 min-w-0 flex flex-col md:flex-row md:items-baseline md:gap-2 pl-2 md:pl-0">
          <span className="text-[14px] md:text-[13px] font-semibold md:font-medium truncate shrink-0" style={{ color: C.textPrimary }}>
            {agent.name}
          </span>
          {/* What this session is currently about. Stacked under the name on a
              phone (where the header is the screen title), inline beside it on
              desktop — the desktop header had no answer to "what is this agent
              working on" at all, and the sidebar row that does is easy to lose
              once the rail is collapsed. */}
          {contextLine && (
            <span className="text-xs truncate min-w-0" style={{ color: C.textMuted }}>
              {contextLine}
            </span>
          )}
        </div>

        {/* Three states, three treatments — and only ONE of them is a claim
            about the session being over. The old two-way badge read `live`,
            which is mtime-based and therefore false for any CLI that is running
            but idle at its prompt; the header then announced "beendet" at a
            session sitting right there. `idle` now gets a quiet dot with no
            word at all: nothing is happening, and nothing is wrong. */}
        {canChat && stream.session && aliveness !== "idle" && (
          <span
            data-testid="session-badge"
            data-aliveness={aliveness}
            className="text-[10px] font-medium px-1.5 py-0.5 rounded font-mono shrink-0"
            style={{
              background: aliveness === "active" ? `${C.online}1A` : C.bgHover,
              color: aliveness === "active" ? C.online : C.textMuted,
              border: `1px solid ${aliveness === "active" ? `${C.online}33` : C.border}`,
            }}
          >
            {aliveness === "active" ? "live" : "beendet"}
          </span>
        )}
        {canChat && stream.session && aliveness === "idle" && (
          <span
            data-testid="session-badge"
            data-aliveness="idle"
            title="Session läuft, wartet am Prompt"
            className="w-1.5 h-1.5 rounded-full shrink-0"
            style={{ background: C.textDim }}
            aria-label="Session läuft, wartet am Prompt"
          />
        )}

        <button
          type="button"
          onClick={() => setOptionsOpen(true)}
          aria-label="Chat-Optionen"
          aria-expanded={optionsOpen}
          className="flex md:hidden items-center justify-center w-10 h-10 shrink-0 rounded-lg cursor-pointer"
          style={{ color: C.textSecondary }}
        >
          <MoreHorizontal size={19} />
        </button>

        <div className="ml-auto hidden md:flex items-center gap-2 shrink-0">
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
          <div
            ref={scrollRef}
            onScroll={handleScroll}
            className="flex-1 min-h-0 overflow-y-auto scroll-quiet flex flex-col pt-2 pb-3"
          >
            {items.length === 0 && stream.pendingEchoes.length === 0 ? (
              stream.loading ? (
                <TimelineSkeleton />
              ) : (
                // Teaches the surface instead of reporting emptiness: a fresh
                // session genuinely has no transcript yet, and the two things
                // the operator can do about it are named.
                <div className="flex flex-1 flex-col items-center justify-center gap-1.5 px-6 text-center">
                  <MessagesSquare size={20} style={{ color: C.textDim }} aria-hidden="true" />
                  <span className="text-[13px] font-medium" style={{ color: C.textSecondary }}>
                    Noch keine Nachrichten
                  </span>
                  <span className="text-[12px] max-w-[42ch]" style={{ color: C.textMuted }}>
                    Schreib unten die erste Nachricht an {agent.name} — oder öffne das Terminal,
                    um die rohe Sitzung zu sehen.
                  </span>
                </div>
              )
            ) : (
              visibleItems.map((item) => {
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
                return renderTimelineEvent(item.event, detailLevel, modelBadges.has(item.event.uuid));
              })
            )}

            {/* Locally-echoed sends, always last: they are by definition the
                newest thing in the conversation, and they disappear the moment
                the transcript confirms them (useChatStream.reconcileEcho). */}
            {stream.pendingEchoes.map((echo) => (
              <ChatMessage
                key={echo.id}
                ev={{
                  kind: "message",
                  uuid: echo.id,
                  ts: new Date(echo.sentAt).toISOString(),
                  role: "user",
                  text: echo.text,
                  model: null,
                  sidechain: false,
                }}
                echoStatus={echo.status}
              />
            ))}
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

          <StatusLine
            state={stream.state}
            connected={stream.connected}
            aliveness={aliveness}
            sending={stream.awaitingResponse}
          />
          <Composer
            agentId={agent.id}
            usage={stream.usage}
            state={stream.state}
            onSend={handleSend}
            onStop={handleStop}
            sessionLive={aliveness !== "ended"}
            capabilities={stream.capabilities}
          />
        </>
      )}

      <ChatOptionsSheet
        open={optionsOpen}
        onClose={() => setOptionsOpen(false)}
        centerView={effectiveView}
        onCenterViewChange={onCenterViewChange}
        canChat={canChat}
        detailLevel={detailLevel}
        onDetailLevelChange={onDetailLevelChange}
        onOpenPanel={onOpenPanel}
      />
    </div>
  );
}
