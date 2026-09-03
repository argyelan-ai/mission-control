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
import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { ChevronDown, ChevronLeft, MessagesSquare, MoreHorizontal } from "lucide-react";
import { C } from "@/lib/colors";
import { api } from "@/lib/api";
import { notify } from "@/lib/notify";
import { useChatStream } from "@/hooks/useChatStream";
import { isAgentStartingError, isNoTranscriptError, resolveAliveness } from "@/lib/chatTypes";
import type { StateEvent, TimelineChatEvent, ToolEvent } from "@/lib/chatTypes";
import { AgentCard } from "./AgentCard";
import { NotificationRow } from "./NotificationRow";
import { isAgentSpawn, matchRuns, notificationsByTool } from "./agentRuns";
import { ChatMessage } from "./ChatMessage";
import { PreviewRow } from "./PreviewRow";
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
import { VoiceButton } from "@/components/voice/VoiceWidget";
import { CENTER_VIEWS, DETAIL_LEVELS, type CenterView, type DetailLevel } from "./chatOptions";
import type { PanelKind } from "./PanelRail";

// Re-exported from their own module so ChatOptionsSheet can use them without
// importing ChatView back (see chatOptions.ts). Importers are unaffected.
export { CENTER_VIEWS, DETAIL_LEVELS };
export type { CenterView, DetailLevel };

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

function isSidechain(ev: TimelineChatEvent): boolean {
  // CommandEvent carries no `sidechain` field (chatTypes.ts) — narrow safely
  // instead of assuming every union member has the property.
  return "sidechain" in ev && ev.sidechain === true;
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

function isActivity(ev: TimelineChatEvent): ev is ActivityEvent {
  return ev.kind === "tool" || ev.kind === "thinking";
}

export type TimelineItem =
  /** A message or command, or a run too short to be worth collapsing. */
  | { kind: "single"; event: TimelineChatEvent }
  /** A run of consecutive tool/thinking events → one ToolGroup chip. */
  | { kind: "activity"; events: ActivityEvent[] }
  /** A run of consecutive sidechain (subagent) events → one SubagentGroup. */
  | { kind: "sidechain"; events: TimelineChatEvent[] }
  /** Ein delegierter Auftrag (Werkzeug `Agent`) → eine eigene Karte. */
  | { kind: "agent"; event: ToolEvent };

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

  const absorbiert = new Set(
    events.filter(isAgentSpawn).map((ev) => ev.toolUseId).filter(Boolean) as string[],
  );

  for (const ev of events) {
    if (ev.kind === "notification" && ev.toolUseId && absorbiert.has(ev.toolUseId)) {
      /* Gehoert zu einer Karte — dort wird sie gezeigt. Zweimal dasselbe
         nebeneinander war genau das Rauschen, das hier weg soll. */
      continue;
    }
    if (isSidechain(ev)) {
      flushActivity();
      sidechainRun.push(ev);
      continue;
    }
    flushSidechain();
    if (isAgentSpawn(ev)) {
      /* Muss VOR der Aktivitaets-Sammlung stehen: sonst verschwindet der
         Auftrag als anonymes "+1 Tool" in einer Werkzeug-Gruppe, weil er in
         der Praxis fast immer neben Bash/Read steht. */
      flushActivity();
      out.push({ kind: "agent", event: ev });
      continue;
    }
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

function renderTimelineEvent(ev: TimelineChatEvent, detailLevel: DetailLevel, showModel = false) {
  switch (ev.kind) {
    case "notification":
      return <NotificationRow key={ev.uuid} ev={ev} />;
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
  const t = useTranslations("sessions");
  const scrollRef = useRef<HTMLDivElement>(null);
  const [stickToBottom, setStickToBottom] = useState(true);
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

  const streamEnabled = hasTranscript && !!agent;
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
    // Basis fuer handleScroll gleich mitfuehren: ohne sie kann das erste
    // Scroll-Ereignis nicht sagen, ob sich Hoehe oder Ansicht geaendert hat.
    lastMetricsRef.current = { top: el.scrollTop, height: el.scrollHeight };
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
      lastMetricsRef.current = { top: el.scrollTop, height: el.scrollHeight };
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, [stickToBottom]);

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
      setStickToBottom(true);
      return;
    }

    const gesture = Date.now() < userDrivingUntilRef.current;
    if (gesture) {
      // Nachlauf: iOS scrollt nach dem Loslassen bis zu ~2s weiter, ohne dass
      // ein weiteres Geraete-Ereignis kommt. Jeder Scroll waehrend einer noch
      // gueltigen Geste haelt sie am Leben.
      markUserDriving();
      setStickToBottom(false);
      return;
    }

    // Ohne Geste: nur eine verschobene ANSICHT zaehlt, kein Hoehensprung.
    const heightChanged = previous != null && previous.height !== height;
    const viewMoved = previous != null && previous.top !== top;
    if (viewMoved && !heightChanged) setStickToBottom(false);
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
      notify.error(t("sendFailed"));
    });
  }

  function handleStop() {
    if (!agent) return;
    api.chat.sendKeys(agent.id, ["Escape"]).catch(() => notify.error(t("stopActionFailed")));
  }

  /** Take a queued steer back from the CLI. Up pops the whole queue into the
   *  terminal's input line, C-u clears that line — proven live on a Docker
   *  agent 03.09.2026 (the withdrawn message was never delivered). The CLI
   *  keeps ONE queue, so every queued message comes back together; with
   *  `edit` their texts land in the composer for another go. */
  function handleWithdrawQueued(edit: boolean) {
    if (!agent) return;
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

  if (!agent) {
    return (
      <div className="flex flex-1 items-center justify-center text-[13px]" style={{ color: C.textMuted }}>
        {t("pickSession")}
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
  // Welches Abzeichen die Kopfzeile gerade zeigt — dieselbe Bedingung wie
  // unten im JSX, hier einmal als Wert, damit die Platzberechnung nicht raet.
  const headerBadge: HeaderBadge = !(canChat && stream.session)
    ? "none"
    : aliveness === "idle"
      ? "dot"
      : aliveness === "active"
        ? "live"
        : "ended";
  const titleSide = headerSideReservation({ hasBack: !!onBack, badge: headerBadge });
  // Die Beschriftung der aktuellen Stufe kommt aus dem Katalog, nicht aus der
  // Liste selbst (chatOptions.ts fuehrt nur Schluessel) — sonst stuende hier
  // wieder ein deutsches Wort in der englischen Oberflaeche.
  const currentDetailLabel = t(
    DETAIL_LEVELS.find((d) => d.key === detailLevel)?.labelKey ?? "detailNormal"
  );

  return (
    <div className="flex flex-col flex-1 min-h-0 overflow-hidden">
      {/* One header for both breakpoints — the parts that only make sense on
          a phone (back chevron, context line, options button) carry
          `md:hidden`, the desktop toolbar carries `hidden md:flex`. Rendering
          two headers would duplicate the agent name in the DOM for no gain. */}
      <div
        data-testid="chat-header"
        // pt-safe-top: auf dem Handy liegt ueber dieser Zeile nichts mehr
        // (AppShell blendet die App-Leiste auf dem Chat-Schirm aus), also muss
        // sie den Notch selbst freihalten — sonst sitzt der Agentenname unter
        // der Uhrzeit des Telefons.
        className="relative flex items-center gap-2 pl-1 pr-2 md:px-4 py-1.5 md:py-2.5 pt-safe-top md:pt-2.5 border-b shrink-0"
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
          {contextLine && (
            <span
              className="text-[11px] md:text-xs truncate max-w-full leading-tight md:min-w-0"
              style={{ color: C.textMuted }}
            >
              {contextLine}
            </span>
          )}
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
        {canChat && stream.session && aliveness !== "idle" && (
          <span
            data-testid="session-badge"
            data-aliveness={aliveness}
            className="relative z-10 text-[10px] font-medium px-1.5 py-0.5 rounded-sm font-mono shrink-0"
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
        <span className="flex md:hidden -m-1">
          <VoiceButton size={36} />
        </span>

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
        </div>
      </div>

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
          className="flex flex-col flex-1 min-h-0 overflow-hidden pb-safe-bottom bg-[var(--color-bg-surface)]"
        >
          <TerminalPanel key={`term-${terminalRemountTick}`} agent={agent} />
        </div>
      ) : (
        <>
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
                    {t("noMessagesYet")}
                  </span>
                  <span className="text-[12px] max-w-[42ch]" style={{ color: C.textMuted }}>
                    {t("noMessagesHint", { name: agent.name })}
                  </span>
                </div>
              )
            ) : (
              visibleItems.map((item) => {
                if (item.kind === "agent") {
                  return (
                    <AgentCard
                      key={item.event.toolUseId ?? item.event.uuid}
                      ev={item.event}
                      run={runMatches.get(item.event.toolUseId ?? "")}
                      notice={toolNotices.get(item.event.toolUseId ?? "")}
                      agentId={agent.id}
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
                onWithdraw={() => handleWithdrawQueued(false)}
                onEdit={() => handleWithdrawQueued(true)}
              />
            ))}

            {/* Die Live-Vorschau kommt zuletzt: sie ist die Antwort, die
                gerade entsteht — nach allem Bestaetigten und nach dem eigenen
                Echo, auf das sie antwortet. */}
            {stream.preview && <PreviewRow preview={stream.preview} />}
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
            prefill={composerPrefill}
            sessionLive={aliveness !== "ended"}
            /* Nur die Docker-tmux-Bruecke liefert echten Pane-Text; Host-Agenten
             * (Boss/Hermes/Jarvis) haben diesen Kanal nicht — dort ist "arbeitet"
             * nie widerlegbar, also bleibt Stop erreichbar. */
            paneObservable={agent.agent_runtime === "cli-bridge"}
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
