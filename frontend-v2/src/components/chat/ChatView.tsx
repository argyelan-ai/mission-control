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
import { Fragment, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { useSearchParams } from "next/navigation";
import { ChevronDown, ChevronLeft, MessagesSquare, MoreHorizontal } from "lucide-react";
import { C, alpha } from "@/lib/colors";
import { api } from "@/lib/api";
import { notify } from "@/lib/notify";
import { useChatStream } from "@/hooks/useChatStream";
import { useHeadTranscript, useHeadTranscriptMeta } from "@/hooks/useHeadTranscript";
import {
  isAgentStartingError,
  isInstallBlockedError,
  isNoTranscriptError,
  resolveSessionAliveness,
} from "@/lib/chatTypes";
import type { ChatTaskStub, StateEvent, TimelineChatEvent } from "@/lib/chatTypes";
import { isHeadDone, type HeadRun } from "@/lib/heads";
import { HeadChatHeader } from "@/components/heads/HeadChatHeader";
import { HeadChatFooter } from "@/components/heads/HeadChatFooter";
import { HeadRunRecordCard } from "@/components/heads/HeadRunRecordCard";
import { HeadNoTranscriptFallback } from "@/components/heads/HeadNoTranscriptFallback";
import { AgentCard } from "./AgentCard";
import { NotificationRow } from "./NotificationRow";
import { WorkingOnRow, type WorkingOn } from "./WorkingOnRow";
import { isAgentSpawn, matchRuns, notificationsByTool } from "./agentRuns";
import type { TimelineItem } from "./buildTimelineItems";
import { ChatMessage } from "./ChatMessage";
import { PreviewRow } from "./PreviewRow";
import { TodoPanel } from "./TodoPanel";
import { ToolRow } from "./ToolRow";
import { ThinkingRow } from "./ThinkingRow";
import { LiveThinkingRow } from "./LiveThinkingRow";
import { SubagentGroup } from "./SubagentGroup";
import { ToolGroup } from "./ToolGroup";
import { ToolOutputContext, createToolOutputStore, type ToolOutputStore } from "./toolOutputStore";
import { ChatMediaContext } from "./chatMedia";
import { ChatResendContext, useResendableErrorUuid, type ChatResend } from "./chatResend";
import { CommandRow } from "./CommandRow";
import { ApprovalCard } from "./ApprovalCard";
import { ElicitationCard } from "./ElicitationCard";
import { ModeChip } from "./ModeChip";
import { StatusLine } from "./StatusLine";
import { Composer } from "./Composer";
import { useIsAdmin } from "@/hooks/useIsAdmin";
import { AdminOnlyNotice } from "@/components/shared/AdminOnlyNotice";
import { TerminalPanel, type AgentWithState } from "./TerminalPanel";
import { ChatOptionsSheet } from "./ChatOptionsSheet";
import { VoiceButton } from "@/components/voice/VoiceWidget";
import { VoiceLiveDock } from "@/components/voice/live/VoiceLive";
import { useVoiceLiveActive } from "@/components/voice/live/voiceLiveActivity";
import { CENTER_VIEWS, DETAIL_LEVELS, type CenterView, type DetailLevel } from "./chatOptions";
import type { PanelKind } from "./PanelRail";
import { useChatTasks } from "@/hooks/useChatTasks";
import { headerAliveness, observedStub, statusLineState } from "./taskSteps/observedState";
import { useTaskSteps, type TaskStepsSpec } from "@/hooks/useTaskSteps";
import { TaskStepsAgentContext } from "@/hooks/useTaskStepDetail";
import { ScrollAnchorContext, type ScrollAnchor } from "./useScrollAnchor";
import { mergeTimeline, type MergeResult, type MergeTask } from "./taskSteps/mergeTimeline";
import { rowMs } from "./taskSteps/buildTaskUnits";
import { SessionDivider, TaskCard, TaskSegment } from "./taskSteps/TaskSection";
import { TaskStepsRetryContext } from "./taskSteps/TaskUnits";

// Re-exported from their own module so ChatOptionsSheet can use them without
// importing ChatView back (see chatOptions.ts). Importers are unaffected.
export { CENTER_VIEWS, DETAIL_LEVELS };
export type { CenterView, DetailLevel };

// buildTimelineItems lebt jetzt in seinem eigenen Modul (Test-Naht: ein Test
// kann die echte Funktion via Modul-Spy zaehlen, waehrend ChatView rendert —
// PR #595 Nacharbeit). Re-export, damit Importeure unverändert bleiben.
export { buildTimelineItems, ACTIVITY_GROUP_MIN_SIZE } from "./buildTimelineItems";
export type { TimelineItem } from "./buildTimelineItems";

// Distance (px) from the bottom of the scroll container within which the
// view still counts as "at the bottom" — classic chat scroll-lock.
const SCROLL_LOCK_THRESHOLD_PX = 48;

/**
 * Wie lange eine Scroll-Geste nachwirkt (ms).
 *
 * Grosszuegig, weil iOS nach dem Loslassen bis zu rund zwei Sekunden
 * weiterscrollt, ohne dass ein weiteres Geraete-Ereignis kommt; jeder Scroll
 * innerhalb des Fensters verlaengert es. Entscheidend ist nicht die Laenge,
 * sondern DASS die Bewaffnung ueberhaupt verfaellt: vorher galt sie unbegrenzt
 * weiter, und ein spaetes Bild-/Schrift-Reflow erbte sie.
 */
const GESTURE_TTL_MS = 2500;

/**
 * Trefferflaeche der beiden runden Handy-Knoepfe in der Kopfzeile.
 *
 * `min-*-touch` sind die 44px-Utilities aus globals.css (DESIGN.md
 * „Mobile-Disziplin: Touch-Targets >= 44px", WCAG 2.5.5). Der SICHTBARE Kreis
 * bleibt 36px und liegt als Kind mittig darin — die zusaetzlichen 8px sind
 * unsichtbar und trotzdem antippbar.
 *
 * `-m-1` (je 4px) ist der Grund, warum der Kopf davon nicht hoeher wird: das
 * negative Aussenmass zieht den 44px-Knopf auf ein Aussenmass von 36px zurueck,
 * die Trefferflaeche ragt in die Polsterung der Kopfzeile hinein statt sie
 * aufzublaehen. Der Kopf bleibt damit bei
 * safe + 6px + 36px + 6px + 1px Linie = safe + 3.0625rem — genau der Wert von
 * `--mobile-chat-topbar-h`, an dem die Handy-Blaetter (Optionen, Kontext) ihre
 * Oberkante ausrichten. Waechst der Kopf, bleibt unter dem Blatt ein Streifen
 * Gespraech stehen. Horizontal gilt dasselbe: der Kreis sitzt exakt dort, wo
 * er vorher sass, die Flaeche reicht nur bis an den Bildschirmrand.
 */
const TOUCH_TARGET = "min-w-touch min-h-touch -m-1";

/**
 * Breiten der Handy-Kopfzeile in px, alle im Browser nachgemessen
 * (Chromium, 390x844, echte JetBrains Mono) — nicht geschaetzt.
 *
 * `control` ist das AUSSENMASS eines runden Knopfes: 44px Trefferflaeche
 * minus `-m-1` auf beiden Seiten (siehe `TOUCH_TARGET`).
 */
export const MOBILE_HEADER_METRICS = {
  headerPaddingLeft: 4, // pl-1
  headerPaddingRight: 8, // pr-2
  control: 36, // Zurueck, Mikrofon, "…"
  gap: 8, // gap-2
  badgeDot: 6, // w-1.5
  badgeLive: 38, // "live"
  badgeEnded: 56, // "beendet"
} as const;

export type HeaderBadge = "none" | "dot" | "live" | "ended";

/**
 * Wie viel Platz die Kopfzeile links und rechts fuer ihre Knoepfe braucht.
 *
 * Vorher stand dort pauschal `px-14` (56px je Seite). Das war zu wenig: die
 * rechte Gruppe belegt mit dem Abzeichen "beendet" 152px, und der Titel lief
 * ihr entgegen — im Browser nachgemessen ragte die Aufgaben-Zeile 50px hinein.
 * Das Abzeichen hat `relative z-10` samt deckendem Hintergrund und malte damit
 * ueber den Namen.
 *
 * BEWUSSTER TAUSCH: die Reservierung ist ASYMMETRISCH, der Name sitzt also
 * mittig im FREIEN Platz und nicht exakt in der Bildmitte. Symmetrisch
 * (2x152px) blieben auf einem 390px-Telefon 86px fuer den Namen uebrig — dann
 * waere "mc-sessions-explore" auf ein paar Zeichen zusammengeschnitten. Den
 * Namen ganz lesen zu koennen wiegt schwerer als optisch exakte Mitte; wer die
 * exakte Mitte will, muss dem Kopf Ballast nehmen (das Abzeichen ist mit bis
 * zu 56px der breiteste Posten).
 */
export function headerSideReservation(opts: {
  hasBack: boolean;
  badge: HeaderBadge;
}): { left: number; right: number } {
  const M = MOBILE_HEADER_METRICS;
  const badgeWidth =
    opts.badge === "ended"
      ? M.badgeEnded
      : opts.badge === "live"
        ? M.badgeLive
        : opts.badge === "dot"
          ? M.badgeDot
          : 0;
  return {
    left: M.headerPaddingLeft + (opts.hasBack ? M.control : 0),
    // Mikrofon + "…" stehen immer, das Abzeichen nur manchmal.
    right:
      M.headerPaddingRight +
      M.control +
      M.gap +
      M.control +
      (badgeWidth > 0 ? M.gap + badgeWidth : 0),
  };
}

function isVisibleAtLevel(ev: TimelineChatEvent, level: DetailLevel): boolean {
  if (level !== "compact") return true;
  /* Ein delegierter Auftrag ist Gespraechsstruktur, kein Werkzeug-Rauschen:
     er sagt, WER hier gerade woran arbeitet. Ohne diese Ausnahme verschwaende
     die Karte in "Kompakt" spurlos — samt dem einzigen Zugang zum Protokoll
     des Subagenten. */
  if (isAgentSpawn(ev)) return true;
  // Kompakt: tool/thinking rows are noise for a quick skim — hide entirely
  // rather than collapse (that's what Normal is for). Since no tool/thinking
  // event survives this filter, `Kompakt` also produces no activity groups —
  // the level's semantics are unchanged by the grouping layer.
  return ev.kind === "message" || ev.kind === "command";
}

/** Timeline items mounted in the first commit — roughly a screenful, so the
 *  operator sees the end of the conversation immediately. */
export const INITIAL_RENDER_WINDOW = 30;

/** At most this long an omp chat waits for its task rows before it shows
 *  what it has (spec A S6). */
export const TASK_LOAD_GATE_MS = 1500;
/** How long a freshly opened chat may say "Loading…" before its first state
 *  frame (round-4 t1 NICE-2: 0.23–0.38 s of ocker "check the terminal" on
 *  every open). Past it, no state is a real unknown again. */
export const FIRST_STATE_GRACE_MS = 4000;

const EMPTY_ROWS: never[] = [];

/** The same array object while its elements are the same objects — so a
 *  memo that depends on it reruns only when one of them really changed. */
function useStableArray<T>(next: T[]): T[] {
  const ref = useRef<T[]>(next);
  const prev = ref.current;
  if (prev !== next && (prev.length !== next.length || prev.some((x, i) => x !== next[i]))) {
    ref.current = next;
  }
  return ref.current;
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
  const t = useTranslations("sessions");
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
      <span className="sr-only">{t("transcriptLoading")}</span>
    </div>
  );
}

/**
 * Welches Ereignis „von aussen" gerade lebt: das juengste mit bekannter
 * Herkunft, und nur solange der Agent arbeitet. Nur dessen Motiv bewegt sich
 * (EventCard/Motif) — ein Verlauf voller kreisender Ringe waere Laerm, ein
 * einzelnes sagt „daran ist er gerade dran". Sobald der Agent still steht,
 * steht auch das Motiv.
 */
export function liveEventUuid(events: TimelineChatEvent[], status: StateEvent["status"] | null): string | null {
  if (status !== "working") return null;
  for (let i = events.length - 1; i >= 0; i--) {
    const ev = events[i];
    if (ev.kind !== "message") continue; // Werkzeuge/Denken = der Agent arbeitet daran
    // Die erste Nachricht von hinten entscheidet: ist es eine Ereignis-Karte,
    // arbeitet der Agent gerade an ihr. Alles andere (Antwort, Operator-Text)
    // heisst: das Ereignis ist beantwortet — es darf nicht mehr pulsieren.
    // omp schreibt die Aufgaben-Nachricht erst am Zug-Ende ins Transcript;
    // ohne diese Regel pulsierte waehrend des Zugs die VORIGE Aufgabe (04.09.).
    return ev.role === "teammate" && ev.source ? ev.uuid : null;
  }
  return null;
}

function renderTimelineEvent(
  ev: TimelineChatEvent,
  detailLevel: DetailLevel,
  showModel = false,
  live = false,
) {
  switch (ev.kind) {
    case "notification":
      return <NotificationRow key={ev.uuid} ev={ev} />;
    case "message":
      return <ChatMessage key={ev.uuid} ev={ev} showModel={showModel} live={live} />;
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
  /** Heads in Chats (heads-sichtbar PR 2, bauplan §3.2) — read-only ONLY
   *  branch: `agent` stays `null` whenever this is set. Switches the whole
   *  body over to `useHeadTranscript`, drops the composer/StatusLine/
   *  Terminal toggle/live-ended badge, and swaps the header for
   *  `HeadChatHeader`. Every other prop below is simply ignored while a
   *  head is shown (ChatView's agent-era contract stays exactly as it
   *  was — nothing here narrows or changes it for an agent). */
  head?: HeadRun | null;
  /** Sidebar-derived, mirrors the backend's fail-closed transcript gate
   *  (resolve_transcript_dir). `false` skips the history/SSE fetch outright
   *  and forces terminal mode — there's nothing to chat with. */
  hasTranscript: boolean;
  /** The chat is actually on screen (default). The sessions page keeps the
   *  last chat mounted but hidden behind the list on the phone — then a live
   *  voice call must not dock into it (nobody could see or hang up), it
   *  floats instead (live finding 13.10.2026). */
  voiceOnScreen?: boolean;
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
  /** The task this agent is bound to right now. Shown as ONE quiet row at
   *  the top of the chat ("Working on <title> → View", `WorkingOnRow`) that
   *  links to the task's own view — the task's session never runs inside
   *  the chat (the chat shows the chat daemon's session only). It replaces
   *  the old task line under the agent name: the same fact twice would
   *  break K3. */
  workingOn?: WorkingOn | null;
  /** Lets the mobile options sheet open the side panels the desktop rail
   *  owns. Omitted = no Panels section in the sheet. */
  onOpenPanel?: (panel: PanelKind) => void;
  /** The run that superseded `head`'s own `needs_you` question (a later run
   *  on the same task, `lib/heads.ts::newerHeadRunIdFor`) — when set,
   *  `HeadChatFooter` shows a link to it instead of the answer field
   *  (review finding on PR #756 round 4: an answered `needs_you` kept
   *  offering "Answer & continue" forever). `null`/omitted for every run
   *  that is not a superseded `needs_you`. */
  newerHeadRunId?: string | null;
  /** Navigates to another head run — used by the "open the newer run" link
   *  above. Omitted alongside `newerHeadRunId` on any caller that never
   *  supplies either. */
  onSelectHeadRun?: (runId: string) => void;
}

export function ChatView({
  agent,
  head = null,
  hasTranscript,
  voiceOnScreen = true,
  detailLevel,
  onDetailLevelChange,
  centerView,
  onCenterViewChange,
  terminalRemountTick = 0,
  onStatusChange,
  onBack,
  workingOn = null,
  onOpenPanel,
  newerHeadRunId = null,
  onSelectHeadRun,
}: ChatViewProps) {
  const t = useTranslations("sessions");
  /** Typing into a live session (text, keys, approvals, effort) is admin-only
   *  on the backend — non-admins read the transcript without a composer. */
  const isAdmin = useIsAdmin();
  const searchParams = useSearchParams();
  const scrollRef = useRef<HTMLDivElement>(null);
  const timelineRef = useRef<HTMLDivElement>(null);
  const [stickToBottom, setStickToBottom] = useState(true);
  /* Mirrors `stickToBottom`, set synchronously wherever it changes (spec A
     M3): the ResizeObserver below reads THIS, not the value captured when it
     was created — otherwise an observer made while following pinned once
     more after an anchored expand had already ended following. */
  const stickRef = useRef(true);
  const setStick = useCallback((value: boolean) => {
    stickRef.current = value;
    setStickToBottom(value);
  }, []);
  const [optionsOpen, setOptionsOpen] = useState(false);
  /** Text eines zurueckgeholten Steers, der noch einmal bearbeitet werden soll. */
  const [composerPrefill, setComposerPrefill] = useState<{ text: string; at: number } | null>(null);
  const [detailOpen, setDetailOpen] = useState(false);
  const detailBoxRef = useRef<HTMLDivElement | null>(null);
  // Chunked first paint: a full Boss transcript is ~200 events of ReactMarkdown,
  // and rendering all of it in one commit blocked the main thread long enough
  // that the page stopped answering (measured: repeated >5s stalls, 741ms LCP
  // render delay). The operator reads the END of the conversation first, so the
  // last screenful is mounted immediately and the rest follows in the next
  // frame. No virtualization library — the list is bounded at MAX_CHAT_EVENTS
  // and this costs one boolean.
  const [renderAll, setRenderAll] = useState(false);

  const streamEnabled = hasTranscript && !!agent && !head;
  // Klick daneben oder Escape schliesst die Detailgrad-Liste. Ohne das bliebe
  // sie offen stehen, waehrend man laengst woanders arbeitet — dasselbe Muster
  // wie beim Modell-Waehler im Composer.
  useEffect(() => {
    if (!detailOpen) return;
    function onPointerDown(e: PointerEvent) {
      if (!detailBoxRef.current?.contains(e.target as Node)) setDetailOpen(false);
    }
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") setDetailOpen(false);
    }
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [detailOpen]);

  // Both hooks run unconditionally (rules of hooks) regardless of which one
  // actually feeds `stream` below — `useChatStream(null, false)` and
  // `useHeadTranscript(null, …)` are each other's safe no-op, so switching
  // between an agent and a head never leaves a disabled hook holding a
  // stale subscription open.
  const agentStream = useChatStream(head ? null : agent?.id ?? null, streamEnabled);
  const headStream = useHeadTranscript(head?.run_id ?? null, head?.state ?? null);
  const stream = head ? headStream : agentStream;
  const voiceCallActive = useVoiceLiveActive(head ? null : agent?.id);
  const headMeta = useHeadTranscriptMeta(head?.run_id ?? null);

  /* Live tool output reaches the rows through a store, not a context value:
     only the row whose tool streams re-renders per snapshot (see
     toolOutputStore.ts). Layout effect, so the snapshot and the scroll pin
     below land in the same frame. */
  const toolOutputStoreRef = useRef<ToolOutputStore | null>(null);
  if (toolOutputStoreRef.current === null) toolOutputStoreRef.current = createToolOutputStore();
  const toolOutputStore = toolOutputStoreRef.current;
  useLayoutEffect(() => {
    toolOutputStore.set(stream.toolOutputs);
  }, [toolOutputStore, stream.toolOutputs]);

  const streamStatus = stream.state?.status ?? null;
  useEffect(() => {
    onStatusChange?.(streamStatus);
  }, [streamStatus, onStatusChange]);

  // Belt and braces: even if the sidebar thought this agent had a
  // transcript, a runtime 404 from the history fetch forces terminal mode
  // the same way a known-no-transcript agent (Hermes/Jarvis) does — no
  // separate dead-end empty-state screen, the toggle just can't reach "chat".
  const canChat = hasTranscript && !isNoTranscriptError(stream.error);
  // Ein ACP-Agent (`headless_chat`) hat keinen zweiten Kopf: die TUI in
  // tmux-Fenster 0 faehrt nichts, was der Operator hier tippt. Der Umschalter
  // entfaellt darum — und mit ihm der gespeicherte `centerView`, der sonst
  // einen Agenten stumm im toten Terminal parken wuerde (ein „terminal" aus
  // einer frueheren Sitzung ueberlebt in localStorage).
  //
  // Der Tiefenlink bleibt: `?view=terminal` ist die bewusste Ausnahme, mit der
  // Betrieb/Health die Konsole weiterhin erreicht (Spec
  // docs/specs/chat-over-acp.md, Nicht-Ziele).
  const headlessChat = !!agent?.headless_chat;
  const terminalDeepLink = searchParams?.get("view") === "terminal";
  // A head never has a terminal (ADR-085 Nachtrag 2026-10-04 §4: files only,
  // no tmux) — forced to "chat" ahead of every other rule below, including
  // `!canChat`, which otherwise exists precisely to FALL BACK to terminal.
  const effectiveView: CenterView = head
    ? "chat"
    : !canChat
      ? "terminal"
      : headlessChat
        ? (terminalDeepLink ? "terminal" : "chat")
        : centerView;

  /* useMemo mit Grund: ein preview-Tick (alle 0.3 s ein replace-me-Event)
     erzeugt ein neues `stream`-Objekt, veraendert aber `events` nicht. Ohne
     Memo liefen filter + buildTimelineItems über den ganzen Verlauf bei JEDEM
     Tick — bei einem grossen Transkript blockte das den Main-Thread so lange,
     dass die Ansicht sprang (Mark: "der chat bewegt sich die ganze zeit").
     `visibleEvents`-Identitaet kommt aus dem Reducer (bei preview unveraendert),
     deshalb ist hier events die Abhaengigkeit, nicht das stream-Objekt. */
  const visibleEvents = useMemo(
    () => stream.events.filter((ev) => isVisibleAtLevel(ev, detailLevel)),
    [stream.events, detailLevel],
  );

  // ── Tasks in the chat (spec A, variant A) ──────────────────────────────
  // An omp agent over ACP runs a task in a session of its own (#898). Its
  // steps stand in THIS chat at their time — as sections, or as a card once
  // finished. Read-only endpoints (`/chat/tasks`, `/chat/tasks/{id}/steps`),
  // never through useChatStream. Client pre-gate (N6): only an agent whose
  // tasks run in sessions of their own asks (`tasks_in_own_session`, the
  // backend's adapter rule — never a harness name here, PRINCIPLES §3.4);
  // every other agent renders at once, exactly as before.
  const agentId = agent?.id ?? null;
  const taskGateOn = !head && !!agent && agent.tasks_in_own_session === true && streamEnabled;
  const chatOnScreen = effectiveView === "chat" && voiceOnScreen;
  // A chat-state flip asks for the stubs at once (inside the hook): a task
  // delivered to an idle agent must not wait for the 60 s idle poll.
  const chatTasks = useChatTasks(agentId, taskGateOn && chatOnScreen, stream.state?.status ?? null);
  const tasksApply = taskGateOn && chatTasks.data?.applies === true;
  const currentStubs: ChatTaskStub[] = useMemo(
    () => (tasksApply && chatTasks.data?.applies ? chatTasks.data.tasks : []),
    [tasksApply, chatTasks.data],
  );

  // What this mount has seen (M4): a task's shape is decided once — finished
  // when first seen = a card, live = a section — and never changes while the
  // view is open; a stub that vanishes keeps its content and says so.
  type Shape = { kind: "card" | "segments"; frozenAtMs: number | null };
  type TaskMemory = {
    agentId: string | null;
    shapes: ReadonlyMap<string, Shape>;
    stubs: ReadonlyMap<string, ChatTaskStub>;
    wasLive: ReadonlySet<string>;
  };
  const [memory, setMemory] = useState<TaskMemory>(() => ({
    agentId, shapes: new Map(), stubs: new Map(), wasLive: new Set(),
  }));
  const mem: TaskMemory =
    memory.agentId === agentId ? memory : { agentId, shapes: new Map(), stubs: new Map(), wasLive: new Set() };
  const defaultShape = (stub: ChatTaskStub): Shape =>
    stub.live ? { kind: "segments", frozenAtMs: null } : { kind: "card", frozenAtMs: rowMs(stub.endedAt) ?? Date.now() };
  useEffect(() => {
    if (!tasksApply) return;
    setMemory((prev) => {
      const base = prev.agentId === agentId ? prev : { agentId, shapes: new Map(), stubs: new Map(), wasLive: new Set<string>() };
      let shapes = base.shapes;
      let wasLive = base.wasLive;
      const stubs = new Map(base.stubs);
      for (const stub of currentStubs) {
        if (!shapes.has(stub.taskId)) shapes = new Map(shapes).set(stub.taskId, defaultShape(stub));
        if (stub.live && !wasLive.has(stub.taskId)) wasLive = new Set(wasLive).add(stub.taskId);
        stubs.set(stub.taskId, stub);
      }
      return { agentId, shapes, stubs, wasLive };
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tasksApply, currentStubs, agentId]);

  // The chat's loaded window (D8): finished tasks older than what the chat
  // shows stay out; live ones always count.
  const oldestEventTs = stream.events.length > 0 ? rowMs(stream.events[0].ts) : null;
  const windowStart = stream.hasMore ? oldestEventTs : rowMs(stream.session?.startedAt ?? null);
  const [openCards, setOpenCards] = useState<ReadonlySet<string>>(() => new Set());
  const toggleCard = useCallback((taskId: string) => {
    setOpenCards((prev) => {
      const next = new Set(prev);
      if (next.has(taskId)) next.delete(taskId);
      else next.add(taskId);
      return next;
    });
  }, []);

  const taskEntries = useMemo(() => {
    if (!tasksApply) return [];
    const current = new Map(currentStubs.map((s) => [s.taskId, s]));
    const list: { stub: ChatTaskStub; shape: Shape; detached: boolean; wasLive: boolean }[] = [];
    for (const stub of currentStubs) {
      const shape = mem.shapes.get(stub.taskId) ?? defaultShape(stub);
      if (!stub.live && shape.kind === "card") {
        const start = rowMs(stub.startKey);
        if (windowStart == null || start == null || start < windowStart) continue;
      }
      list.push({ stub, shape, detached: false, wasLive: mem.wasLive.has(stub.taskId) || stub.live });
    }
    // Vanished in this mount after being live here: keep what was shown.
    for (const [id, stub] of mem.stubs) {
      if (current.has(id) || !mem.wasLive.has(id)) continue;
      const shape = mem.shapes.get(id) ?? defaultShape(stub);
      list.push({ stub: { ...stub, live: false }, shape, detached: true, wasLive: true });
    }
    list.sort((a, b) => (rowMs(a.stub.startKey) ?? 0) - (rowMs(b.stub.startKey) ?? 0));
    return list;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tasksApply, currentStubs, mem, windowStart]);

  const stepSpecs: TaskStepsSpec[] = useMemo(
    () =>
      taskEntries.map(({ stub, shape, detached }) => ({
        taskId: stub.taskId,
        live: stub.live && !detached,
        status: stub.status,
        load: shape.kind === "segments" || stub.live || openCards.has(stub.taskId),
      })),
    [taskEntries, openCards],
  );
  const steps = useTaskSteps(agentId, stepSpecs, taskGateOn && chatOnScreen);
  const stepsRef = useRef(steps);
  stepsRef.current = steps;
  const retrySteps = useCallback((taskId: string) => stepsRef.current.get(taskId)?.retry(), []);
  // One version per poll result, so the memos below see a change only when
  // some task's state object really changed (structural sharing upstream).
  const stepStates = taskEntries.map((e) => steps.get(e.stub.taskId)?.state ?? null);
  const stepsVersion = useStableArray(stepStates);
  // Failed with nothing loaded: the section says so instead of a bare head.
  const stepFailures = useStableArray(
    taskEntries.map((e) => {
      const st = steps.get(e.stub.taskId);
      return !!st && st.error && !st.state;
    }),
  );

  // The task's state from the freshest source (round-3 t1 NICE-2/3): the
  // steps poll (2 s) carries the status too and is usually ahead of the stub
  // poll (10 s). Same object while nothing changes, so the memos below hold.
  const observedRef = useRef(new Map<string, { base: ChatTaskStub; seen: ChatTaskStub }>());
  const observed = useStableArray(
    taskEntries.map(({ stub, detached }) => {
      if (detached) return stub;
      const st = steps.get(stub.taskId);
      const seen = observedStub(stub, st?.state ?? null, chatTasks.updatedAt, st?.updatedAt ?? 0);
      if (seen === stub) return stub;
      const prev = observedRef.current.get(stub.taskId);
      if (
        prev &&
        prev.base === stub &&
        prev.seen.status === seen.status &&
        prev.seen.live === seen.live &&
        prev.seen.startedAt === seen.startedAt
      ) {
        return prev.seen;
      }
      observedRef.current.set(stub.taskId, { base: stub, seen });
      return seen;
    }),
  );

  const mergeTasks: MergeTask[] = useMemo(
    () =>
      taskEntries.map(({ shape, detached, wasLive }, i) => {
        const stub = observed[i];
        const state = stepsVersion[i];
        // A card that was never live in this mount keeps all its rows; one that
        // went live again shows the new work below it (M4).
        const frozenAtMs = shape.kind === "card" ? (wasLive && !detached ? shape.frozenAtMs : Number.POSITIVE_INFINITY) : null;
        return {
          stub,
          shape: shape.kind,
          frozenAtMs,
          rows: state?.rows ?? EMPTY_ROWS,
          liveRow: stub.live && !detached ? state?.liveRow ?? null : null,
          omitted: state?.omitted ?? 0,
          // Not loaded yet: no "waiting for the first step" flash.
          hasSession: state ? state.hasSession : true,
          detached,
          loadFailed: stepFailures[i],
          endLine: !stub.live && !detached && (shape.kind === "segments" || wasLive),
        };
      }),
    [taskEntries, stepsVersion, stepFailures, observed],
  );

  // The newest chapter of a live task opens by itself — only while the view
  // follows; otherwise the open chapter stays frozen (no height change above
  // the reader, M3).
  const newestChapterIds = useMemo(() => {
    const ids = new Set<string>();
    for (const t of mergeTasks) {
      if (!t.stub.live || t.detached) continue;
      for (let i = t.rows.length - 1; i >= 0; i--) {
        if (t.rows[i].k === "think") {
          ids.add(t.rows[i].id);
          break;
        }
      }
    }
    return ids;
  }, [mergeTasks]);
  const [frozenAutoIds, setFrozenAutoIds] = useState<ReadonlySet<string>>(() => new Set());
  useEffect(() => {
    if (stickToBottom) setFrozenAutoIds(newestChapterIds);
  }, [stickToBottom, newestChapterIds]);
  const autoIds = stickToBottom ? newestChapterIds : frozenAutoIds;

  const queued = tasksApply && chatTasks.data?.applies ? chatTasks.data.queued : 0;
  const waitingModel = stream.state?.status === "waiting_model";
  // Previous result only for object reuse (N5) — the output never depends on it.
  const prevMergeRef = useRef<MergeResult | null>(null);
  const merged = useMemo(
    () =>
      mergeTimeline({
        chatEvents: visibleEvents,
        tasks: mergeTasks,
        detailLevel,
        queued,
        autoIds,
        hidePending: waitingModel,
        prev: prevMergeRef.current,
      }),
    [visibleEvents, mergeTasks, detailLevel, queued, autoIds, waitingModel],
  );
  useLayoutEffect(() => {
    prevMergeRef.current = merged;
  }, [merged]);
  const taskById = useMemo(() => new Map(mergeTasks.map((t) => [t.stub.taskId, t])), [mergeTasks]);
  const liveTask = useMemo(() => [...mergeTasks].reverse().find((t) => t.stub.live && !t.detached) ?? null, [mergeTasks]);
  // A review run of this agent works a card (never live: it stays in review).
  const reviewRun = useMemo(() => currentStubs.some((s) => s.reviewRun === true), [currentStubs]);
  // The task turn ended (no live task left) — the chat probe lags behind it.
  const statusState = useMemo(
    () =>
      statusLineState(stream.state, {
        tasksShown: tasksApply,
        tasksKnown: chatTasks.settled && !chatTasks.error,
        liveTask: liveTask != null,
        reviewRun,
      }),
    [stream.state, tasksApply, chatTasks.settled, chatTasks.error, liveTask, reviewRun],
  );
  // The first moments of a chat: no state frame yet is "loading", not
  // "unknown" (round-4 t1 NICE-2) — for FIRST_STATE_GRACE_MS per agent.
  const [firstStateGraceOver, setFirstStateGraceOver] = useState(false);
  useEffect(() => {
    setFirstStateGraceOver(false);
    const id = setTimeout(() => setFirstStateGraceOver(true), FIRST_STATE_GRACE_MS);
    return () => clearTimeout(id);
  }, [agentId]);
  const firstStatePending = stream.state == null && !firstStateGraceOver;

  // Load gate (S6): an omp chat shows its timeline once history, task stubs
  // and the first steps page of each live task are in — at most 1.5 s — so
  // the tail-first commit already holds the task rows and no card is
  // inserted above the reader after mount.
  const [gateCapPassed, setGateCapPassed] = useState(false);
  useEffect(() => {
    setGateCapPassed(false);
    if (!taskGateOn) return;
    const id = setTimeout(() => setGateCapPassed(true), TASK_LOAD_GATE_MS);
    return () => clearTimeout(id);
  }, [taskGateOn, agentId]);
  const liveStepsSettled = mergeTasks.every((t) => !t.stub.live || t.detached || !!steps.get(t.stub.taskId)?.settled);
  const gateReady =
    !taskGateOn || gateCapPassed || (!stream.loading && chatTasks.settled && (!tasksApply || liveStepsSettled));

  // `renderAll` is in the deps for a reason: when the deferred remainder mounts,
  // content appears ABOVE the viewport, so a scroll position left untouched
  // would silently show older messages instead of the end. `stream.preview`
  // likewise: the live preview grows without touching `events` (Befund
  // 10.09.2026 — the view stayed put while the preview added 400px below it).
  useEffect(() => {
    if (!stickToBottom) return;
    const el = scrollRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
    // Basis fuer handleScroll gleich mitfuehren: ohne sie kann das erste
    // Scroll-Ereignis nicht sagen, ob sich Hoehe oder Ansicht geaendert hat.
    lastMetricsRef.current = { top: el.scrollTop, height: el.scrollHeight };
  }, [
    stream.events,
    stream.preview,
    stream.toolOutputs,
    // The echo's own height is pinned too (M1); task rows grow without
    // touching `stream.events`.
    stream.pendingEchoes.length,
    merged,
    gateReady,
    stickToBottom,
    renderAll,
    effectiveView,
  ]);

  // One frame later, not on a timer: the browser gets to paint the tail first,
  // which is the whole point.
  useEffect(() => {
    // Behind the load gate nothing is drawn yet: the frame counts from the
    // moment the timeline really mounts.
    if (renderAll || !gateReady) return;
    if (typeof requestAnimationFrame === "undefined") {
      setRenderAll(true);
      return;
    }
    const handle = requestAnimationFrame(() => setRenderAll(true));
    return () => cancelAnimationFrame(handle);
  }, [renderAll, gateReady]);

  // The effect above fires on new events, which is not enough: the mobile
  // stack keeps the off-screen pane mounted with `display: none`, where the
  // container measures 0 and "scroll to the bottom" is a no-op. Nothing then
  // changes when it becomes visible, so the timeline opened at the very top of
  // a 6000px history. Observing the container's own box catches that, plus
  // every later resize (window, rotation, on-screen keyboard).
  //
  // The CONTENT box is observed as well: late reflows (markdown, images,
  // fonts, a preview growing line by line) change the content height without
  // any new event and without the container moving. Only the content box sees
  // those. The container stays the first observed element — the gesture tests
  // rely on that order.
  useEffect(() => {
    const el = scrollRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => {
      if (!stickRef.current) return;
      el.scrollTop = el.scrollHeight;
      lastMetricsRef.current = { top: el.scrollTop, height: el.scrollHeight };
    });
    observer.observe(el);
    if (timelineRef.current) observer.observe(timelineRef.current);
    return () => observer.disconnect();
    // `effectiveView`: the scroll container only exists in chat view. Started
    // in the terminal (or forced there by a transcript 404 that clears later),
    // the first run finds no element — and nothing else re-runs this effect.
  }, [stickToBottom, effectiveView]);

  // Das Mitlaufen wird NUR durch eine echte Geste beendet — nie durch ein
  // Scroll-Ereignis allein (Operator-Befund 20.08.2026: "beginnt ganz am
  // Anfang des Gespraechs").
  //
  // Warum die Unterscheidung noetig ist: Zuerst rendern nur die letzten
  // ``INITIAL_RENDER_WINDOW`` Eintraege, einen Frame spaeter mounten ALLE —
  // und zwar OBERHALB des Sichtfensters. Die Hoehe springt dabei von ein paar
  // hundert auf mehrere tausend Pixel, ``scrollTop`` bleibt stehen, und der
  // Browser feuert fuer diesen Umbruch ein ``scroll``-Ereignis. Das sah
  // vorher exakt aus wie "der Nutzer hat hochgescrollt": der Abstand zum Ende
  // war riesig, das Mitlaufen wurde abgeschaltet, und der Effekt, der ans Ende
  // gesprungen waere, fand es bereits aus vor. Ergebnis: die Ansicht stand am
  // Anfang einer langen Historie. Ein Wettlauf — darum trat er bei kurzen
  // Verlaeufen nie auf und wurde mit jedem gewachsenen Gespraech schlimmer.
  //
  // Ein Scroll-Ereignis kann diese Frage grundsaetzlich nicht beantworten:
  // Layout-Umbrueche und Nutzer-Gesten erzeugen dasselbe Ereignis. Also wird
  // die Absicht dort gelesen, wo sie eindeutig ist — am Eingabegeraet.
  // Zwei Loecher im ersten Anlauf (Review 20.08.2026), beide hier behoben:
  //
  // 1. `onPointerDown` bewaffnete die Geste bei JEDEM Antippen — Nachricht
  //    antippen zum Kopieren, "Mehr anzeigen", Fehlgriff. Abgeraeumt wurde nur
  //    im `atBottom`-Zweig. Einmal bewaffnet, galt der naechste layoutbedingte
  //    Scroll wieder als Geste. Jetzt zaehlt erst eine BEWEGUNG bei gedruecktem
  //    Zeiger, und die Bewaffnung VERFAELLT.
  // 2. Ein Scroll OHNE Geste liess das Mitlaufen an — auch wenn die Ansicht
  //    dabei wirklich woanders hinsprang (Seitensuche im Browser,
  //    `scrollIntoView`, wiederhergestellte Scroll-Position). Beim naechsten
  //    Ereignis wurde der Operator vom Text weggerissen, den er gerade las.
  //
  // Der zweite Punkt ist ohne Eingabegeraet entscheidbar, und zwar sicherer als
  // ueber Gesten: ein Layout-Umbruch aendert die HOEHE (der Inhalt waechst
  // oberhalb, `scrollTop` bleibt stehen), eine verschobene Ansicht aendert den
  // SCROLLTOP bei gleicher Hoehe. Nur waehrend laufender Ausgabe aendert sich
  // beides gleichzeitig — dort entscheidet weiterhin die Geste.
  const userDrivingUntilRef = useRef(0);
  const pointerDraggingRef = useRef(false);
  const lastMetricsRef = useRef<{ top: number; height: number } | null>(null);

  function markUserDriving() {
    userDrivingUntilRef.current = Date.now() + GESTURE_TTL_MS;
  }

  // Nur gedrueckte Bewegung ist eine Scroll-Absicht. Ein Antippen erzeugt
  // `pointerdown` ohne nennenswerte Bewegung und darf nichts bewaffnen; die
  // Rollbalken-Geste mit der Maus erzeugt weder `wheel` noch `touchmove` und
  // muss trotzdem zaehlen.
  function handlePointerDown() {
    pointerDraggingRef.current = true;
  }
  function handlePointerMove(e: React.PointerEvent) {
    if (pointerDraggingRef.current && e.buttons !== 0) markUserDriving();
  }
  function handlePointerUp() {
    pointerDraggingRef.current = false;
  }

  function handleScroll() {
    const el = scrollRef.current;
    if (!el) return;
    const top = el.scrollTop;
    const height = el.scrollHeight;
    const previous = lastMetricsRef.current;
    lastMetricsRef.current = { top, height };

    const distanceFromBottom = height - top - el.clientHeight;
    const atBottom = distanceFromBottom < SCROLL_LOCK_THRESHOLD_PX;

    // Zurueck ans Ende gilt immer — egal wodurch. Wer unten steht, will
    // mitlaufen, und die Geste ist damit beendet.
    if (atBottom) {
      userDrivingUntilRef.current = 0;
      setStick(true);
      return;
    }

    const gesture = Date.now() < userDrivingUntilRef.current;
    if (gesture) {
      // Nachlauf: iOS scrollt nach dem Loslassen bis zu ~2s weiter, ohne dass
      // ein weiteres Geraete-Ereignis kommt. Jeder Scroll waehrend einer noch
      // gueltigen Geste haelt sie am Leben.
      markUserDriving();
      setStick(false);
      return;
    }

    // Ohne Geste: nur eine verschobene ANSICHT zaehlt, kein Hoehensprung.
    const heightChanged = previous != null && previous.height !== height;
    const viewMoved = previous != null && previous.top !== top;
    if (viewMoved && !heightChanged) setStick(false);
  }

  function handleSend(text: string) {
    if (!agent) return;
    // Echo BEFORE the request: the bubble and the scroll happen in this frame,
    // not a second later when the tailer has polled the transcript. The request
    // failing removes the echo again — an echo that outlived a failed send would
    // be the one thing worse than the old delay.
    // Sending jumps to the end first (M1): from anywhere up in a long task
    // section the operator otherwise never sees his own message arrive.
    jumpToBottom();
    stream.echoSent(text, stream.state?.status === "working");
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
      if (isInstallBlockedError(err)) {
        // A real, named refusal — not a transport failure — so it gets its
        // own honest wording instead of the generic "Send failed".
        notify.error(t("installBlocked"));
        return;
      }
      notify.error(t("sendFailed"));
    });
  }

  async function handleStop() {
    if (!agent) return;
    if (headlessChat) {
      // The chat daemon holds follow-ups behind the running reply and would
      // start the next one the moment this turn is cancelled. Stop means stop:
      // take them back first, into the composer — the way Claude Code and
      // Codex hand queued input back on interrupt. If the daemon cannot (older
      // agent image, no answer), the rows stay — it still holds them and will
      // send them — and the cancel goes out regardless.
      const hadQueued = stream.pendingEchoes.some((e) => e.status === "queued");
      const dropped = await takeBackHeadlessQueue({ quiet: !hadQueued });
      if (dropped && dropped.length > 0) {
        setComposerPrefill({ text: dropped.join("\n\n"), at: Date.now() });
      }
    }
    api.chat.sendKeys(agent.id, ["Escape"]).catch(() => notify.error(t("stopActionFailed")));
  }

  /** The voice stopped the turn (voice PR 4). MC already took the queue
   *  back from the daemon (`takenBack`, `null` when it could not or the
   *  agent is a TUI); drop the local queued rows and refill the composer. */
  function handleVoiceStopped(takenBack: string[] | null) {
    if (takenBack === null) return;
    stream.withdrawQueued();
    if (takenBack.length > 0) setComposerPrefill({ text: takenBack.join("\n\n"), at: Date.now() });
  }

  /** Takes the follow-ups a headless agent's daemon still holds back and
   *  drops their local rows — only once the daemon has answered. `null` when
   *  it could not: the rows stay (the daemon still holds the messages) and,
   *  unless `quiet`, the operator is told (review #777). */
  async function takeBackHeadlessQueue({ quiet = false } = {}): Promise<string[] | null> {
    if (!agent) return null;
    let dropped: string[];
    try {
      dropped = (await api.chat.clearQueue(agent.id)).dropped ?? [];
    } catch {
      if (!quiet) notify.error(t("queueTakeBackFailed"));
      return null;
    }
    stream.withdrawQueued();
    return dropped;
  }

  /** Take a queued message back. On a TUI agent Up pops the CLI's whole queue
   *  into the terminal's input line and C-u clears that line — proven live on
   *  a Docker agent 03.09.2026 (the withdrawn message was never delivered).
   *  On a headless (ACP) agent the chat daemon holds the queue; it answers
   *  with what it still held, which is the truth for the composer (a message
   *  that already started as a turn does not come back twice). Either way
   *  every queued message comes back together; with `edit` their texts land
   *  in the composer for another go. */
  async function handleWithdrawQueued(edit: boolean) {
    if (!agent) return;
    if (headlessChat) {
      const dropped = await takeBackHeadlessQueue();
      if (edit && dropped && dropped.length > 0) {
        setComposerPrefill({ text: dropped.join("\n\n"), at: Date.now() });
      }
      return;
    }
    const taken = stream.withdrawQueued();
    if (taken.length === 0) return;
    api.chat.sendKeys(agent.id, ["Up", "C-u"]).catch(() => notify.error(t("stopActionFailed")));
    if (edit) setComposerPrefill({ text: taken.join("\n\n"), at: Date.now() });
  }

  function handleAnswer(key: string) {
    if (!agent) return;
    // Digit alone, no trailing Enter — numbered pickers accept the bare key.
    api.chat.sendKeys(agent.id, [key]).catch(() => notify.error(t("answerFailed")));
  }

  /* Einmal je Verlauf, nicht je Karte: die Zuordnung laeuft ueber ALLE
     Ereignisse (nicht nur die sichtbaren), damit ein in "Kompakt"
     ausgeblendetes Werkzeug keinen Lauf freigibt, der einem spaeteren
     Aufruf faelschlich zufiele. */
  const runMatches = useMemo(
    () => matchRuns(stream.events, stream.subagentRuns),
    [stream.events, stream.subagentRuns],
  );
  /* Meldungen, die zu einem Werkzeugaufruf gehoeren, werden in dessen Karte
     gezeigt — nicht zusaetzlich als eigene Zeile daneben. */
  const toolNotices = useMemo(() => notificationsByTool(stream.events), [stream.events]);
  // What a live voice call listens along to (voice PR 3): the same stream the
  // chat renders. A preview tick changes none of these three, so it costs nothing.
  const voiceProgress = useMemo(
    () => ({ state: stream.state, events: stream.events, todos: stream.todos }),
    [stream.state, stream.events, stream.todos],
  );

  // "Nach unten"-Knopf (S7): neu ist, was eine NEUERE Zeit traegt als das
  // Neueste beim Loslassen — Chat-Eintraege, Aufgaben-Schritte und Echos.
  // Keine Laengen-Differenz: eine spaet eintreffende Seite alter Schritte
  // oder ein geteiltes Kapitel aendert die Laenge, ohne dass etwas neu ist.
  const newestAtUnstickRef = useRef<{ server: number | null; echo: number } | null>(null);
  useEffect(() => {
    newestAtUnstickRef.current = stickToBottom ? null : { server: merged.newestTs, echo: Date.now() };
    // Nur beim Umschalten einfrieren, nicht bei jedem neuen Eintrag.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stickToBottom]);
  const unstick = newestAtUnstickRef.current;
  const newSinceUnstick =
    stickToBottom || unstick == null
      ? 0
      : (unstick.server == null ? merged.countNewer(Number.NEGATIVE_INFINITY) : merged.countNewer(unstick.server)) +
        stream.pendingEchoes.filter((e) => e.sentAt > unstick.echo).length;

  // Tail first; the remainder joins one frame later (see `renderAll`).
  const visibleEntries = useMemo(
    () => (renderAll ? merged.entries : merged.entries.slice(-INITIAL_RENDER_WINDOW)),
    [merged, renderAll],
  );
  const queuedLabel = useCallback((count: number) => t("taskQueued", { count }), [t]);

  // Is a head of the live task's section on screen? (S5) Then the status
  // line stays silent in `task_working` (the head says it, K3); otherwise it
  // names the task. One observer; the state changes only on a crossing.
  const [taskHeadVisible, setTaskHeadVisible] = useState(false);
  const liveTaskId = liveTask?.stub.taskId ?? null;
  useEffect(() => {
    const root = scrollRef.current;
    if (!root || !liveTaskId || typeof IntersectionObserver === "undefined") {
      setTaskHeadVisible(false);
      return;
    }
    const heads = Array.from(
      root.querySelectorAll(`[data-task-id="${liveTaskId}"] [data-region="chat-task-head"]`),
    );
    const seen = new Map<Element, boolean>();
    const observer = new IntersectionObserver(
      (records) => {
        for (const r of records) seen.set(r.target, r.isIntersecting);
        setTaskHeadVisible([...seen.values()].some(Boolean));
      },
      { root },
    );
    heads.forEach((h) => observer.observe(h));
    if (heads.length === 0) setTaskHeadVisible(false);
    return () => observer.disconnect();
  }, [liveTaskId, merged, gateReady, effectiveView]);

  // Was der Agent GERADE tut, aus dem Transkript statt vom Bildschirm
  // (Befund 10.09.2026): der Tailer sendet ein Werkzeug-Ereignis sofort beim
  // Aufruf (result null) und noch einmal, sobald das Ergebnis da ist. Ist das
  // letzte Ereignis ein Werkzeug ohne Ergebnis, laeuft es jetzt.
  const activity = useMemo(() => {
    const last = stream.events[stream.events.length - 1];
    if (last && last.kind === "tool" && last.result == null && !last.sidechain) {
      return { title: last.title };
    }
    return null;
  }, [stream.events]);

  // Fallback for `HeadChatFooter`'s "needs you" question: `run.question`
  // (question.md) is the real answer, but the rare run that ended before
  // writing one still needs the operator to see SOMETHING here rather than
  // a bare answer field (review finding on PR #756) — the transcript's own
  // last assistant message is the next best thing.
  const lastAssistantMessage = useMemo(() => {
    for (let i = stream.events.length - 1; i >= 0; i--) {
      const ev = stream.events[i];
      if (ev.kind === "message" && ev.role === "assistant" && ev.text?.trim()) return ev.text.trim();
    }
    return null;
  }, [stream.events]);

  /* Anchored toggles (spec A §6.3, M3): the tapped row keeps its place on
     screen — the same scroll owner, no second follow logic. */
  const anchorRef = useRef<{ el: Element; top: number } | null>(null);
  const scrollAnchor = useMemo<ScrollAnchor>(
    () => ({
      capture(el) {
        const box = scrollRef.current;
        if (!box || !el) return;
        setStick(false);
        anchorRef.current = { el, top: el.getBoundingClientRect().top - box.getBoundingClientRect().top };
      },
      restore() {
        const a = anchorRef.current;
        const box = scrollRef.current;
        anchorRef.current = null;
        if (!a || !box || !a.el.isConnected) return;
        const now = a.el.getBoundingClientRect().top - box.getBoundingClientRect().top;
        if (now !== a.top) box.scrollTop += now - a.top;
        lastMetricsRef.current = { top: box.scrollTop, height: box.scrollHeight };
      },
    }),
    [setStick],
  );

  function jumpToBottom() {
    const el = scrollRef.current;
    if (!el) return;
    userDrivingUntilRef.current = 0;
    el.scrollTop = el.scrollHeight;
    lastMetricsRef.current = { top: el.scrollTop, height: el.scrollHeight };
    setStick(true);
  }
  const modelBadges = modelBadgeUuids(visibleEvents);
  const liveUuid = liveEventUuid(visibleEvents, stream.state?.status ?? null);
  // Single source for how alive this session is — the LIVE state frame wins
  // over the history handshake, because the history query no longer refetches
  // on focus while the pane probe keeps publishing (see resolveSessionAliveness).
  const aliveness = resolveSessionAliveness(stream);
  const prompt = stream.state?.status === "permission_prompt" ? stream.state.prompt : null;

  /* "Send again" on a chat error card (S1-R7, chatResend.tsx): the card sends
     through handleSend — echo + deliver, the composer's own path, never a
     second one — and only the newest card holding the operator's text is
     offered, while nothing was sent after it. Where the composer cannot send
     (a head's transcript, a non-admin, an ended session) nothing is offered. */
  const handleSendRef = useRef(handleSend);
  handleSendRef.current = handleSend;
  const canSend = !head && !!agent && isAdmin && aliveness !== "ended";
  const resendUuid = useResendableErrorUuid(stream.events, stream.pendingEchoes, canSend);
  const chatResend = useMemo<ChatResend>(
    () => ({
      resend: (text) => handleSendRef.current(text),
      canResend: (uuid) => uuid === resendUuid,
    }),
    [resendUuid],
  );
  // Welches Abzeichen die Kopfzeile gerade zeigt — dieselbe Bedingung wie
  // unten im JSX, hier einmal als Wert, damit die Platzberechnung nicht raet.
  // "active" comes from the transcript's age (written < 60 s ago); the status
  // line from the live state. Right after a reply the two disagreed — "live"
  // in the header, "Bereit" below — and seconds later the same state showed
  // the dot (round-1 NICE-2). A chat the status line calls ready gets the dot.
  // Round 4 (t1 NICE-1/4): from the state the status line shows, not the raw
  // probe — that one kept "live" 12.9 s over a line that said "Ready".
  const badgeAliveness = headerAliveness(aliveness, statusState);
  const headerBadge: HeaderBadge = !(canChat && stream.session)
    ? "none"
    : badgeAliveness === "idle"
      ? "dot"
      : badgeAliveness === "active"
        ? "live"
        : "ended";
  const titleSide = headerSideReservation({ hasBack: !!onBack, badge: headerBadge });
  // Die Beschriftung der aktuellen Stufe kommt aus dem Katalog, nicht aus der
  // Liste selbst (chatOptions.ts fuehrt nur Schluessel) — sonst stuende hier
  // wieder ein deutsches Wort in der englischen Oberflaeche.
  const currentDetailLabel = t(
    DETAIL_LEVELS.find((d) => d.key === detailLevel)?.labelKey ?? "detailNormal"
  );

  // Phone action bar (mobile nav V2): Back · Reply · More at the bottom,
  // within thumb reach; the header on top stays for orientation. "Reply"
  // focuses the composer synchronously inside the tap — iOS only opens the
  // keyboard for a focus that happens in the gesture itself.
  const rootRef = useRef<HTMLDivElement>(null);

  // Moved here, AFTER every hook above (review finding on PR #756): every
  // value computed since `stream` is already null/empty-safe for
  // `agent: null, head: null` (`stream` itself comes from
  // `useChatStream(null, false)`/`useHeadTranscript(null, null)`, each
  // other's documented no-op), so gating only the JSX — not the hook
  // calls — costs nothing. Guarding with an early `return` BEFORE the
  // `useMemo`/`useRef`/`useEffect` calls above (the previous shape) broke
  // the Rules of Hooks: the very first real-world case that re-renders
  // this component from "no head yet" to "a head" on the SAME mount — the
  // Archive sheet's own "tap a row" path (the run is not in the recent
  // list, so `head` arrives one tick late from `sessions/page.tsx`'s
  // fallback fetch) and every `?head=` deep link (the list has not loaded
  // yet on page load) — threw "Rendered more hooks than during the
  // previous render" (reproduced directly: render with `head={null}`, then
  // re-render the SAME `ChatView` instance with a real `head`).
  function renderChatItem(item: TimelineItem) {
                if (item.kind === "agent") {
                  return (
                    <AgentCard
                      key={item.event.toolUseId ?? item.event.uuid}
                      ev={item.event}
                      run={runMatches.get(item.event.toolUseId ?? "")}
                      notice={toolNotices.get(item.event.toolUseId ?? "")}
                      // A head has no subagent-detail endpoint of its own
                      // (ADR-085 Nachtrag §4: the main transcript only) — an
                      // empty id just makes the "open subagent" affordance a
                      // harmless no-reply instead of crashing on `agent.id`.
                      agentId={agent?.id ?? ""}
                    />
                  );
                }
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
                if (item.event.kind === "elicitation") {
                  return (
                    <ElicitationCard
                      key={item.event.uuid}
                      ev={item.event}
                      result={stream.elicitationResults[item.event.elicitationId] ?? null}
                      // Read-only for a head's transcript and for a non-admin
                      // (the answer endpoint is operator-only, like the composer).
                      agentId={head || !isAdmin ? null : agent?.id ?? null}
                    />
                  );
                }
                return renderTimelineEvent(
                  item.event,
                  detailLevel,
                  modelBadges.has(item.event.uuid),
                  item.event.uuid === liveUuid,
                );
  }

  if (!agent && !head) {
    return (
      <div className="flex flex-1 items-center justify-center text-[13px]" style={{ color: C.textMuted }}>
        {t("pickSession")}
      </div>
    );
  }

  return (
    <ToolOutputContext.Provider value={toolOutputStore}>
    <ScrollAnchorContext.Provider value={scrollAnchor}>
    <TaskStepsAgentContext.Provider value={agentId}>
    <TaskStepsRetryContext.Provider value={retrySteps}>
    {/* P2: tool pictures load from THIS agent's media route; a head (no
        agent row) shows none rather than guessing an address. */}
    <ChatMediaContext.Provider value={agent?.id ?? null}>
    <ChatResendContext.Provider value={chatResend}>
    <div ref={rootRef} className="flex flex-col flex-1 min-h-0 overflow-hidden">
      {/* One header for both breakpoints — the parts that only make sense on
          a phone (back chevron, options button) carry
          `md:hidden`, the desktop toolbar carries `hidden md:flex`. Rendering
          two headers would duplicate the agent name in the DOM for no gain.
          A head gets its OWN, much smaller header (`HeadChatHeader`) instead
          of a branch threaded through this one — see that component's own
          docstring for why. */}
      {head ? (
        <HeadChatHeader head={head} onBack={onBack} />
      ) : agent ? (
      <div
        data-testid="chat-header"
        // pt-safe-top: auf dem Handy liegt ueber dieser Zeile nichts mehr
        // (AppShell blendet die App-Leiste auf dem Chat-Schirm aus), also muss
        // sie den Notch selbst freihalten — sonst sitzt der Agentenname unter
        // der Uhrzeit des Telefons.
        //
        // Deckende Flaeche (Operator-Befund 19.09.2026): der Kopf hatte gar
        // keine eigene — sichtbar war bisher nur der Grund der Ahnen
        // (`chat-column`), und der Notch-Streifen, den `pt-safe-top`
        // freihaelt, wurde vom Plattform-Backdrop durch den transparenten
        // Kopf hindurchgemalt. Beim Ueberziehen (iOS-Rubber-Band) liest sich
        // Inhalt darin durch. `padding-top` liegt INNERHALB der
        // Hintergrundbox: dieselbe Utility-Klasse deckt den Streifen mit ab.
        // Token, nicht Farbwert — beide Themes. Kein Inline-Stil: der
        // schluege jede Klasse und damit spaetere md-Varianten.
        className="relative flex items-center gap-2 pl-1 pr-2 md:px-4 py-1.5 md:py-2.5 pt-safe-top md:pt-2.5 border-b shrink-0 bg-[var(--color-bg-surface)]"
        style={{ borderColor: C.border }}
      >
        {onBack && (
          <button
            type="button"
            onClick={onBack}
            aria-label={t("backToSessions")}
            // Rund statt eckig (Operator-Wunsch 19.08.2026, Vorbild
            // Claude-App): ein runder Knopf liest sich als Navigation, ein
            // eckiger als Schaltflaeche im Inhalt.
            //
            // Sichtbarer Kreis 36px, TREFFERFLAECHE 44px (DESIGN.md/WCAG
            // 2.5.5) — siehe `TOUCH_TARGET` oben fuer die Rechnung, warum der
            // Kopf davon nicht hoeher wird.
            className={`relative z-10 flex md:hidden items-center justify-center shrink-0 cursor-pointer ${TOUCH_TARGET}`}
          >
            <span
              className="flex items-center justify-center w-9 h-9 rounded-full"
              style={{ color: C.textSecondary, backgroundColor: C.bgHover }}
            >
              <ChevronLeft size={19} />
            </span>
          </button>
        )}

        {/* EIN Titel-Block, der nur seine Anordnung wechselt — nicht zwei
            Bloecke mit `md:hidden`/`hidden md:`. Zwei waeren derselbe Name
            zweimal im Dokument: Vorleseprogramme lesen ihn doppelt, und jede
            Suche nach dem Namen findet zwei Treffer (genau daran ist der
            erste Versuch hier aufgelaufen).

            Auf dem Handy liegt er ABSOLUT in der Mitte, nicht im Fluss
            zwischen den Knoepfen: im Fluss haenge seine Position davon ab,
            wie breit links und rechts gerade sind (Badge da oder nicht) — der
            Name wuerde je nach Zustand wandern. `px-14` haelt ihn von den
            beiden runden Knoepfen frei, `pointer-events-none` aus ihrem Weg.

            Ab md kehrt er in den Fluss zurueck: dort ist der Kopf eine
            Werkzeugleiste, kein Bildschirmtitel, und ein mittiger Name haette
            neben den Schaltern rechts keinen Bezugspunkt. */}
        <div
          data-testid="chat-header-title"
          // Die Reservierung kommt als CSS-Variable herein, NICHT als
          // Inline-Padding: ein Inline-Stil schlaegt jede Klasse, auch
          // `md:px-0` — auf dem Desktop bliebe der Handy-Platzhalter stehen.
          // Als Variable in einer Utility-Klasse gilt die md-Regel wieder.
          style={
            {
              "--chat-title-l": `${titleSide.left}px`,
              "--chat-title-r": `${titleSide.right}px`,
            } as React.CSSProperties
          }
          // `items-center` IST die Zentrierung auf dem Handy: der Block ist
          // eine Spalte, seine Kinder sind schmaler als er. `justify-center`
          // und `text-center` standen hier frueher mit — beide wirkungslos
          // (im Browser nachgemessen: Name identisch bei x=120.4/w=149.2),
          // weil der Block `absolute` ohne `top`/`bottom` ist und seine Hoehe
          // exakt dem Inhalt entspricht. Es gibt nichts zu verteilen.
          className="absolute inset-x-0 pl-[var(--chat-title-l)] pr-[var(--chat-title-r)]
                     flex flex-col items-center pointer-events-none
                     md:static md:inset-auto md:px-0 md:flex-1 md:min-w-0 md:flex-row md:items-baseline
                     md:gap-2 md:pointer-events-auto"
        >
          <span
            className="text-[15px] md:text-[13px] font-semibold md:font-medium truncate max-w-full md:shrink-0"
            style={{ color: C.textPrimary }}
          >
            {agent.name}
          </span>
        </div>

        {/* Rechte Gruppe: EIN ml-auto buendelt Badge und "…" am rechten Rand.
            Zwei getrennte auto-Margins wuerden den freien Platz unter sich
            aufteilen und das Badge in die Naehe der Mitte schieben — genau
            dorthin, wo der Titel liegt. */}
        <div className="ml-auto flex items-center gap-2 shrink-0">
        {/* Three states, three treatments — and only ONE of them is a claim
            about the session being over. The old two-way badge read `live`,
            which is mtime-based and therefore false for any CLI that is running
            but idle at its prompt; the header then announced "beendet" at a
            session sitting right there. `idle` now gets a quiet dot with no
            word at all: nothing is happening, and nothing is wrong. */}
        {headerBadge === "live" || headerBadge === "ended" ? (
          <span
            data-testid="session-badge"
            data-aliveness={badgeAliveness}
            className="relative z-10 text-[10px] font-medium px-1.5 py-0.5 rounded-sm font-mono shrink-0"
            style={{
              background: headerBadge === "live" ? alpha(C.online, 0.1) : C.bgHover,
              color: headerBadge === "live" ? C.online : C.textMuted,
              border: `1px solid ${headerBadge === "live" ? alpha(C.online, 0.2) : C.border}`,
            }}
          >
            {headerBadge === "live" ? "live" : "beendet"}
          </span>
        ) : null}
        {headerBadge === "dot" && (
          <span
            data-testid="session-badge"
            data-aliveness="idle"
            title={t("sessionWaitingAtPrompt")}
            className="relative z-10 w-1.5 h-1.5 rounded-full shrink-0"
            style={{ background: C.textDim }}
            aria-label={t("sessionWaitingAtPrompt")}
          />
        )}

        {/* Sprachbedienung. Auf dem Chat-Schirm gibt es die App-Leiste nicht
            mehr (AppShell `mobileChromeless`), und ihr Knopf war der EINZIGE
            Zugang auf dem Handy: die Sidebar ist `hidden md:flex`, ein
            Tastenkuerzel existiert nicht. Ohne diesen Ersatz waere die
            Sprachbedienung auf dem Telefon ersatzlos weggefallen — ausgerechnet
            dort, wo Sprechen am meisten bringt.

            Warum in den Kopf und nicht ins Optionen-Blatt: Sprache ist ein
            Griff fuer besetzte Haende. Zwei Antippen bis zum Mikrofon nehmen
            ihm genau den Vorteil. Der Knopf bringt seine 44px-Trefferflaeche
            selbst mit; `-m-1` am Rahmen haelt den Kopf auf seiner Hoehe,
            dieselbe Rechnung wie bei `TOUCH_TARGET`. */}
        {/* While a live voice call runs in THIS chat, the Jarvis mic steps
            aside: two mics with two meanings side by side confused the
            operator (voice PR 1 review). Jarvis itself is unchanged. */}
        {!voiceCallActive && (
          <span className="flex md:hidden -m-1">
            <VoiceButton size={36} />
          </span>
        )}

        <button
          type="button"
          onClick={() => setOptionsOpen(true)}
          aria-label={t("chatOptions")}
          aria-expanded={optionsOpen}
          className={`relative z-10 flex md:hidden items-center justify-center shrink-0 cursor-pointer ${TOUCH_TARGET}`}
        >
          <span
            className="flex items-center justify-center w-9 h-9 rounded-full"
            style={{ color: C.textSecondary, backgroundColor: C.bgHover }}
          >
            <MoreHorizontal size={18} />
          </span>
        </button>
        </div>

        <div className="hidden md:flex items-center gap-2 shrink-0">
          {/* Detailgrad: EIN Knopf mit Klappliste statt drei Segmenten
              (Operator-Wunsch 19.08.2026: "diese viele buttons und switche
              rechts oben irgendwie verpacken"). Die Wahl der Bedienform folgt
              der Benutzungshaeufigkeit — den Detailgrad stellt man einmal ein,
              Chat/Terminal schaltet man staendig. Darum klappt genau dieser
              ein und der Umschalter daneben bleibt offen. */}
          {effectiveView === "chat" && (
            <div className="relative" ref={detailBoxRef}>
              <button
                type="button"
                data-testid="detail-level-trigger"
                aria-haspopup="listbox"
                aria-expanded={detailOpen}
                aria-label={t("detailLevelCurrent", { level: currentDetailLabel })}
                onClick={() => setDetailOpen((v) => !v)}
                className="inline-flex items-center gap-1 px-2.5 py-1.5 text-[10px] font-medium rounded-md cursor-pointer transition-colors whitespace-nowrap"
                style={{
                  border: `1px solid ${C.border}`,
                  background: detailOpen ? C.bgHover : "transparent",
                  color: detailOpen ? C.textPrimary : C.textMuted,
                }}
              >
                {currentDetailLabel}
                <ChevronDown
                  size={10}
                  className="transition-transform duration-150"
                  style={{ transform: detailOpen ? "rotate(180deg)" : undefined }}
                />
              </button>
              {detailOpen && (
                <div
                  role="listbox"
                  aria-label={t("detailLevel")}
                  className="absolute top-full right-0 mt-1 w-32 rounded-lg overflow-hidden z-20 p-1"
                  style={{
                    backgroundColor: C.bgElevated,
                    border: `1px solid ${C.border}`,
                    boxShadow: "var(--shadow-elevated)",
                  }}
                >
                  {DETAIL_LEVELS.map(({ key, labelKey }) => (
                    <button
                      key={key}
                      type="button"
                      role="option"
                      aria-selected={detailLevel === key}
                      onClick={() => {
                        onDetailLevelChange(key);
                        setDetailOpen(false);
                      }}
                      className="w-full text-left px-2 py-1.5 text-[11px] rounded-md cursor-pointer transition-colors hover:bg-[var(--color-bg-hover)]"
                      // Die Flaeche der NICHT gewaehlten Eintraege bleibt
                      // bewusst ohne Inline-Stil. Ein Inline-Stil schlaegt
                      // jede Klasse, auch `hover:` — mit
                      // `backgroundColor: "transparent"` war der Hover ein
                      // Nichts (im Browser gegengeprueft: gehovert meldet
                      // getComputedStyle rgba(0,0,0,0) statt der Hover-Farbe).
                      // Genau die Falle, vor der der Kaskaden-Hinweis in
                      // globals.css warnt.
                      style={{
                        color: detailLevel === key ? C.accent : C.textPrimary,
                        ...(detailLevel === key
                          ? { backgroundColor: C.accentSubtle }
                          : null),
                      }}
                    >
                      {t(labelKey)}
                    </button>
                  ))}
                </div>
              )}
            </div>
          )}

          {!headlessChat && (
          <div
            className="flex items-center rounded-md overflow-hidden"
            style={{ border: `1px solid ${C.border}` }}
          >
            {CENTER_VIEWS.map(({ key, labelKey }) => {
              const disabled = key === "chat" && !canChat;
              return (
                <button
                  key={key}
                  type="button"
                  disabled={disabled}
                  onClick={() => onCenterViewChange(key)}
                  aria-pressed={effectiveView === key}
                  title={disabled ? t("noTranscript") : undefined}
                  className="px-2.5 py-1.5 text-[10px] font-medium transition-colors cursor-pointer disabled:cursor-not-allowed disabled:opacity-40 whitespace-nowrap"
                  style={{
                    background: effectiveView === key ? C.accentSubtle : "transparent",
                    color: effectiveView === key ? C.accent : C.textMuted,
                    borderRight: key !== "terminal" ? `1px solid ${C.border}` : undefined,
                  }}
                >
                  {t(labelKey)}
                </button>
              );
            })}
          </div>
          )}
        </div>
      </div>
      ) : null}

      {/* Only where the chat shows no task sections: there the section head
          names the task (K3). */}
      {!head && agent && workingOn && !tasksApply && (!taskGateOn || chatTasks.settled) && (
        <WorkingOnRow taskId={workingOn.taskId} title={workingOn.title} />
      )}

      {effectiveView === "terminal" ? (
        // Home-Balken freihalten. Die Tab-Leiste trug den Zuschlag
        // (`env(safe-area-inset-bottom)`); `mobileChromeless` nimmt sie weg,
        // und der Ersatz sitzt im Composer — den es hier gar nicht gibt.
        // Ohne diesen Rahmen liegt die unterste Terminal-Zeile unter dem
        // Balken. Betrifft auch jeden Agenten ohne Transkript, der ueber
        // `canChat` fest im Terminal steht (Hermes, Jarvis).
        //
        // Der Zuschlag sitzt hier und nicht am Chat-Container: der Composer
        // soll den Streifen bewusst MITBENUTZEN (die Pille laeuft in ihn
        // hinein), das Terminal darf es nicht.
        //
        // Gleiche Flaechenfarbe wie das Panel, sonst stuende unter dem
        // Terminal ein andersfarbiger Streifen.
        <div
          data-testid="terminal-safe-area"
          className={`flex flex-col flex-1 min-h-0 overflow-hidden pb-safe-bottom bg-[var(--color-bg-surface)]`}
        >
          {/* `agent!`: this branch only renders when `effectiveView ===
              "terminal"`, which is forced to `"chat"` whenever `head` is
              set (above) — so reaching here means `head` is falsy, and the
              function's own early return already guarantees `agent` is set
              whenever `head` is not. TypeScript cannot see that cross-
              variable guarantee on its own. */}
          <TerminalPanel key={`term-${terminalRemountTick}`} agent={agent!} />
        </div>
      ) : (
        <>
          <div className="relative flex-1 min-h-0 flex flex-col">
          <div
            ref={scrollRef}
            onScroll={handleScroll}
            // Die Geste, nicht ihre Folge: Rad, Finger und die Navigations-
            // tasten sagen eindeutig, dass der Mensch scrollt. Ein
            // ``scroll``-Ereignis allein sagt das nicht (siehe handleScroll).
            onWheel={markUserDriving}
            onTouchMove={markUserDriving}
            onKeyDown={markUserDriving}
            onPointerDown={handlePointerDown}
            onPointerMove={handlePointerMove}
            onPointerUp={handlePointerUp}
            onPointerCancel={handlePointerUp}
            className="flex-1 min-h-0 overflow-y-auto scroll-quiet flex flex-col pb-3"
          >
          {/* The top gap lives INSIDE the timeline, not on the scroller: a
              sticky task head sticks at the scroller's padding edge, and an
              8 px scroller padding let the rows passing beneath it peek out
              above the head (phone, 31.10.2026). */}
          <div ref={timelineRef} data-testid="chat-timeline" className="flex flex-col flex-1 pt-2">
            {!gateReady ? (
              <TimelineSkeleton />
            ) : merged.entries.length === 0 && stream.pendingEchoes.length === 0 ? (
              stream.loading ? (
                <TimelineSkeleton />
              ) : head ? (
                <HeadNoTranscriptFallback runId={head.run_id} reason={headMeta.reason} historyFailed={!!stream.error} />
              ) : (
                // Teaches the surface instead of reporting emptiness: a fresh
                // session genuinely has no transcript yet, and the two things
                // the operator can do about it are named.
                <div className="flex flex-1 flex-col items-center justify-center gap-1.5 px-6 text-center">
                  <MessagesSquare size={20} style={{ color: C.textDim }} aria-hidden="true" />
                  <span className="text-[13px] font-medium" style={{ color: C.textSecondary }}>
                    {t("noMessagesYet")}
                  </span>
                  <span className="text-[12px] max-w-[42ch]" style={{ color: C.textMuted }}>
                    {t("noMessagesHint", { name: agent?.name ?? "" })}
                  </span>
                </div>
              )
            ) : (
              visibleEntries.map((entry, i) => {
                // The merge's own key: a task row can split a chat tool group,
                // and both halves would share renderChatItem's turn-uuid key.
                if (entry.kind === "chat") {
                  // Chat lines right after a task band: the chat's own session
                  // goes on here, not the task's (31.10.2026).
                  const back = i > 0 && visibleEntries[i - 1].kind === "segment";
                  return (
                    <Fragment key={entry.key}>
                      {back && <SessionDivider kind="back" />}
                      {renderChatItem(entry.item)}
                    </Fragment>
                  );
                }
                const task = taskById.get(entry.taskId);
                if (!task) return null;
                if (entry.kind === "card") {
                  const st = steps.get(entry.taskId);
                  return (
                    <TaskCard
                      key={entry.key}
                      stub={task.stub}
                      open={openCards.has(entry.taskId)}
                      onToggle={toggleCard}
                      steps={st?.state ?? null}
                      loading={!st?.state && !st?.error}
                      error={!!st?.error}
                      onRetry={retrySteps}
                      frozenAtMs={task.frozenAtMs}
                      detailLevel={detailLevel}
                    />
                  );
                }
                // A task's first band starts a session of its own.
                return (
                  <Fragment key={entry.key}>
                    {entry.first && <SessionDivider kind="start" />}
                    <TaskSegment entry={entry} stub={task.stub} detailLevel={detailLevel} queuedLabel={queuedLabel} />
                  </Fragment>
                );
              })
            )}
            {/* The newest band is followed by the chat's own echo or live
                preview — the same session boundary as above. */}
            {visibleEntries.length > 0 && visibleEntries[visibleEntries.length - 1].kind === "segment" &&
              (stream.pendingEchoes.length > 0 || stream.preview?.source === "acp") && <SessionDivider kind="back" />}

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
                harness={agent?.harness ?? null}
                headless={headlessChat}
                onWithdraw={() => handleWithdrawQueued(false)}
                onEdit={() => handleWithdrawQueued(true)}
              />
            ))}

            {/* Die Live-Vorschau kommt zuletzt: sie ist die Antwort, die
                gerade entsteht — nach allem Bestaetigten und nach dem eigenen
                Echo, auf das sie antwortet. NUR aus ACP-Text-Deltas: der
                tmux-Bildschirmabgriff ("pane") zeigte Bruchstuecke, Werkzeug-
                Rahmen und den Dispatch-Prompt und ist seit 10.09.2026 nicht
                mehr Teil des Chats — die Statuszeile sagt, was laeuft. */}
            {/* Ein laufender GEDANKE ist keine Antwort (20.10.2026: minutenlanges
                Denken sah aus wie „nichts passiert"): eigene, eingeklappte
                Zeile mit Uhr und Token-Schaetzung statt der Antwort-Vorschau. */}
            {stream.preview && stream.preview.source === "acp" && (
              stream.preview.block === "thinking"
                ? <LiveThinkingRow preview={stream.preview} />
                : <PreviewRow preview={stream.preview} />
            )}

            {/* The run-record summary card — only once the head has ended
                AND actually wrote one (`run.run_record`, the same boolean
                `HeadStateCard` already reads). Last in the timeline, same
                place `pendingEchoes`/`preview` claim for an agent: it is the
                newest fact about this conversation. */}
            {/* `isHeadDone`, not "not active": `needs_you` is a FINAL state
                too (it is not in `HEAD_ACTIVE_STATES`) but it is not DONE —
                the operator is still blocked on it, and the record card
                would show whatever a run writes for itself at step 0
                ("pending"/zeroed counters) as if it were the real result
                (review finding on PR #756 round 4). */}
            {head && isHeadDone(head) && head.run_record && <HeadRunRecordCard runId={head.run_id} />}
          </div>
          </div>

          {!stickToBottom && (
            <button
              type="button"
              onClick={jumpToBottom}
              className="absolute bottom-3 right-4 z-10 inline-flex items-center gap-1.5 rounded-full border px-3 py-1.5 text-[12px] font-medium transition-colors hover:bg-[var(--color-bg-hover)] focus-visible:outline-none focus-visible:ring-1"
              style={{
                background: C.bgElevated,
                borderColor: C.borderActive,
                color: C.textPrimary,
              }}
            >
              <ChevronDown size={14} aria-hidden="true" />
              <span>{t("jumpToBottom")}</span>
              {newSinceUnstick > 0 && (
                <span
                  className="rounded-full px-1.5 py-px text-[11px] tabular-nums"
                  style={{ background: C.accent, color: C.onAccent }}
                >
                  {t("newMessagesCount", { count: newSinceUnstick })}
                </span>
              )}
            </button>
          )}
          </div>

          {prompt && isAdmin && (
            <div className="px-3 pt-2">
              <ApprovalCard
                prompt={prompt}
                onAnswer={handleAnswer}
                onShowTerminal={() => onCenterViewChange("terminal")}
                // The voice asks for a tap; the card shows where (voice PR 4).
                attention={voiceCallActive}
              />
            </div>
          )}

          {/* The agent's todo list, pinned under the conversation (agents and
              heads alike): one line collapsed, the full list on tap. */}
          <TodoPanel todo={stream.todos} />

          {head ? (
            // No StatusLine (that line is "is the AGENT's terminal alive",
            // meaningless for a head) and no Composer at all — the footer
            // below is the one surface a head's chat shows instead.
            <HeadChatFooter
              run={head}
              transcriptFallbackQuestion={lastAssistantMessage}
              newerRunId={newerHeadRunId}
              onOpenNewerRun={onSelectHeadRun}
              onRestarted={onSelectHeadRun}
            />
          ) : (
            <>
              <StatusLine
                state={statusState}
                connected={stream.connected}
                activity={activity}
                aliveness={aliveness}
                sending={stream.awaitingResponse}
                task={liveTask ? { taskId: liveTask.stub.taskId, title: liveTask.stub.title, deliveredAt: liveTask.stub.deliveredAt } : null}
                taskHeadVisible={taskHeadVisible}
                loading={firstStatePending}
                hasTerminal={!headlessChat}
              />
              {!isAdmin ? (
                <AdminOnlyNotice compact message={t("inputAdminOnly")} />
              ) : agent ? (
                <VoiceLiveDock
                  agentId={agent.id}
                  agentName={agent.name}
                  // Delivered by MC already — only the echo, no second send.
                  onForwarded={(text, midTurn) => {
                    jumpToBottom();
                    stream.echoSent(text, midTurn);
                  }}
                  // Stopped by voice: same aftermath as the Stop button —
                  // the follow-ups the daemon held go back into the composer.
                  onStopped={handleVoiceStopped}
                  progress={voiceProgress}
                  onScreen={voiceOnScreen}
                >
                {(voiceSlot) => (
                <Composer
                  voiceSlot={voiceSlot}
                  agentId={agent.id}
                  usage={stream.usage}
                  state={stream.state}
                  onSend={handleSend}
                  onStop={handleStop}
                  prefill={composerPrefill}
                  placeholder={liveTask ? t("messagePlaceholderTaskRunning") : undefined}
                  sessionLive={aliveness !== "ended"}
                  /* Nur die Docker-tmux-Bruecke liefert echten Pane-Text; Host-Agenten
                   * (Boss/Hermes/Jarvis) haben diesen Kanal nicht — dort ist "arbeitet"
                   * nie widerlegbar, also bleibt Stop erreichbar. */
                  paneObservable={agent.agent_runtime === "cli-bridge"}
                  capabilities={stream.capabilities}
                  modeSlot={
                    headlessChat ? (
                      <ModeChip
                        agentId={agent.id}
                        capabilities={stream.capabilities}
                        liveMode={stream.mode}
                        busy={
                          stream.state?.status === "working" ||
                          stream.state?.status === "waiting_input"
                        }
                      />
                    ) : null
                  }
                />
                )}
                </VoiceLiveDock>
              ) : null}
            </>
          )}
        </>
      )}

      {!head && (
        <ChatOptionsSheet
          open={optionsOpen}
          onClose={() => setOptionsOpen(false)}
          centerView={effectiveView}
          onCenterViewChange={onCenterViewChange}
          canChat={canChat}
          headlessChat={headlessChat}
          detailLevel={detailLevel}
          onDetailLevelChange={onDetailLevelChange}
          onOpenPanel={onOpenPanel}
        />
      )}
    </div>
    </ChatResendContext.Provider>
    </ChatMediaContext.Provider>
    </TaskStepsRetryContext.Provider>
    </TaskStepsAgentContext.Provider>
    </ScrollAnchorContext.Provider>
    </ToolOutputContext.Provider>
  );
}
