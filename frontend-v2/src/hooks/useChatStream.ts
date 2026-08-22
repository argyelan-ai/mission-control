"use client";

import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { useSSE } from "@/lib/sse";
import type {
  SubagentRun,
  ChatCapabilities,
  ChatEvent,
  ChatSession,
  StateEvent,
  TimelineChatEvent,
  UsageEvent,
} from "@/lib/chatTypes";

// ── Optimistic echo ──────────────────────────────────────────────────────────
//
// A sent message used to appear only once the tailer had polled the transcript
// (1s interval) and the CLI had written the line — so the operator's own words
// took over a second to show up, which is what "fühlt sich nicht snappy an"
// was about. The echo renders the bubble locally the instant it is sent and
// steps aside when the real transcript event lands.
//
// It is deliberately NOT pushed through `chatReducer`: that reducer is a pure
// projection of the transcript, and mixing a local guess into it would make
// "what the agent actually recorded" unanswerable.

/** How long an echo may stay unacknowledged before it says so. Generous on
 *  purpose — the floor is the tailer's 1s poll plus however long the CLI takes
 *  to flush the line. Only counted while the agent is NOT mid-turn; see
 *  `markUnconfirmedEchoes`. */
export const ECHO_CONFIRM_TIMEOUT_MS = 10_000;

/** Wait before the single automatic retry of a send the backend rejected with
 *  `agent_starting`. One retry, not a loop: if the agent still isn't up, that is
 *  news the operator should get, not something to paper over indefinitely. */
export const ECHO_RETRY_DELAY_MS = 10_000;

/**
 * `pending`     — sent, waiting for the transcript. The ordinary case.
 * `queued`      — sent while the agent was mid-turn. The CLI genuinely queues
 *                 the line and only writes it once the running turn ends, so
 *                 there is nothing wrong and nothing to warn about.
 * `starting`    — the backend said the agent is still coming up
 *                 (`agent_starting`); one automatic retry is scheduled.
 * `unconfirmed` — the timeout passed while the agent was idle AND the stream
 *                 healthy, so the message may never have reached the CLI.
 *                 Truthful, not a spinner.
 */
export type EchoStatus = "pending" | "queued" | "starting" | "unconfirmed";

export interface PendingEcho {
  /** Local id — never a transcript uuid, so it can't collide with one. */
  id: string;
  text: string;
  sentAt: number;
  status: EchoStatus;
  /** True once the single `agent_starting` retry has been used up. */
  retried?: boolean;
}

/** Kommandos, deren ERFOLG die Sitzung wegwirft. Ihre Bestaetigung kann per
 *  Definition nicht im Transkript stehen — die CLI legt eine neue Datei an, und
 *  in der taucht die abgeschickte Zeile nie auf. Der Sitzungswechsel selbst IST
 *  hier der Beweis (Operator-Befund 19.08.2026: nach /clear bei Boss stand
 *  "Nicht bestaetigt — Terminal pruefen", obwohl /clear sauber lief).
 *  Bewusst eng gehalten: nur Kommandos, bei denen der Rollover der Zweck ist.
 *  /compact steht NICHT drin - es verdichtet innerhalb derselben Session. */
const SESSION_CLEARING_COMMANDS = ["/clear"];

export function isSessionClearingCommand(text: string): boolean {
  const first = text.trim().split(/\s+/)[0]?.toLowerCase() ?? "";
  return SESSION_CLEARING_COMMANDS.includes(first);
}

/** Retires echoes that a session rollover has just ANSWERED: exactly the
 *  session-clearing commands above. Alles andere bleibt unangetastet — ein
 *  Rollover aus anderem Grund (Recycler, Auto-Compact der CLI) darf eine
 *  normale Nachricht nicht faelschlich als zugestellt abhaken (die
 *  Rollover-Haerte aus R14a bleibt damit gewahrt). */
export function retireEchoesAnsweredByRollover(echoes: PendingEcho[]): PendingEcho[] {
  const next = echoes.filter((e) => !isSessionClearingCommand(e.text));
  return next.length === echoes.length ? echoes : next;
}

/** Echo states that are simply waiting on something known and expected — no
 *  warning belongs on any of them. */
export function isCalmEchoStatus(status: EchoStatus): boolean {
  return status !== "unconfirmed";
}

/** Whitespace-insensitive compare: the CLI echoes back what it received, but a
 *  trailing newline or a re-wrapped paste must still count as the same message. */
function sameMessage(a: string, b: string): boolean {
  const norm = (s: string) => s.replace(/\s+/g, " ").trim();
  return norm(a) === norm(b);
}

/**
 * Retires the echo that an incoming transcript user-message corresponds to.
 *
 * Prefers a text match. Falls back to the OLDEST echo when nothing matches,
 * because the two failure modes are not symmetric: retiring an echo early only
 * removes a local copy while the real message is on screen anyway, whereas
 * keeping it would show the same message twice — visible, and it looks broken.
 * (The operator typing directly in the terminal is the case that hits the
 * fallback; losing a still-pending echo there is harmless.)
 *
 * Pure so the rule can be tested without React.
 */
export function reconcilePendingEchoes(
  echoes: PendingEcho[],
  incomingText: string,
  { allowOldestFallback = true }: { allowOldestFallback?: boolean } = {},
): PendingEcho[] {
  if (echoes.length === 0) return echoes;
  const matched = echoes.findIndex((e) => sameMessage(e.text, incomingText));
  if (matched >= 0) return echoes.filter((_, i) => i !== matched);
  // The fallback is only safe for a LIVE user event, which almost certainly is
  // our own send arriving reformatted. A bulk history scan is different: after a
  // session rollover the refetched page is full of unrelated user messages, and
  // falling back there would retire a still-queued echo against a stranger —
  // claiming a delivery that hasn't happened.
  if (!allowOldestFallback) return echoes;
  return echoes.slice(1);
}

/** Removes the newest echo with this text — a failed send is always the most
 *  recent one, and nothing was delivered, so the bubble must go. */
export function withdrawPendingEcho(echoes: PendingEcho[], text: string): PendingEcho[] {
  const reversedIdx = [...echoes].reverse().findIndex((e) => sameMessage(e.text, text));
  if (reversedIdx < 0) return echoes;
  const idx = echoes.length - 1 - reversedIdx;
  return echoes.filter((_, i) => i !== idx);
}

/**
 * Flips echoes that have gone unacknowledged past the timeout — but ONLY while
 * the agent is not mid-turn.
 *
 * This is the bug the operator hit: a message sent while the agent is working is
 * QUEUED by the CLI and reaches the transcript only after the running turn ends.
 * A turn can easily outlast ten seconds, so the timer declared "nicht
 * bestätigt" at a message that was sitting safely in the queue. While the agent
 * works, echoes read `queued` instead and the clock does not run at all; it
 * resumes once the agent is idle, where an unacknowledged message really is
 * suspicious.
 *
 * `starting` is likewise never flipped: it has its own retry in flight.
 * Returns the same array when nothing changed, so callers can skip a re-render.
 */
export function markUnconfirmedEchoes(
  echoes: PendingEcho[],
  now: number,
  agentWorking = false,
): PendingEcho[] {
  let changed = false;
  const next = echoes.map((e) => {
    if (agentWorking) {
      // Mid-turn: show the queue, don't start a clock.
      if (e.status === "pending") {
        changed = true;
        return { ...e, status: "queued" as const };
      }
      return e;
    }
    // Turn over: a queued line should now be written, so it goes back to
    // waiting-and-counting rather than staying "queued" forever.
    if (e.status === "queued") {
      changed = true;
      return { ...e, status: "pending" as const };
    }
    if (e.status === "pending" && now - e.sentAt >= ECHO_CONFIRM_TIMEOUT_MS) {
      changed = true;
      return { ...e, status: "unconfirmed" as const };
    }
    return e;
  });
  return changed ? next : echoes;
}

/** Marks an echo as waiting on a starting agent and records that its one retry
 *  is still available. */
export function markEchoStarting(echoes: PendingEcho[], text: string): PendingEcho[] {
  const reversedIdx = [...echoes].reverse().findIndex((e) => sameMessage(e.text, text));
  if (reversedIdx < 0) return echoes;
  const idx = echoes.length - 1 - reversedIdx;
  const next = echoes.slice();
  next[idx] = { ...next[idx], status: "starting" };
  return next;
}

/** Records that the single automatic retry for this echo has been spent. */
export function markEchoRetried(echoes: PendingEcho[], text: string): PendingEcho[] {
  const reversedIdx = [...echoes].reverse().findIndex((e) => sameMessage(e.text, text));
  if (reversedIdx < 0) return echoes;
  const idx = echoes.length - 1 - reversedIdx;
  const next = echoes.slice();
  next[idx] = { ...next[idx], status: "pending", retried: true };
  return next;
}

// ── Reducer ──────────────────────────────────────────────────────────────────
//
// Pure — no I/O, no React. `useChatStream` below feeds it both the initial
// history page (one dispatch per event) and every live "chat_event" SSE
// frame; both paths go through the exact same reducer, so replaying history
// and tailing live traffic behave identically.

export const MAX_CHAT_EVENTS = 1500;

export interface ChatReducerState {
  events: TimelineChatEvent[];
  /** Dedup/replace lookup key -> index into `events`. See `eventKey` for why
   *  this isn't simply the event's `uuid`. */
  index: Map<string, number>;
  state: StateEvent | null;
  usage: UsageEvent | null;
  /** Bumped every time a `session_changed` event is processed. The hook
   *  watches this to know a history refetch is needed — it can't rely on
   *  `events` being empty as the signal, since a second rollover could land
   *  before the first refetch re-seeds anything. */
  sessionChangedAt: number;
}

export function createInitialChatState(): ChatReducerState {
  return { events: [], index: new Map(), state: null, usage: null, sessionChangedAt: 0 };
}

/**
 * Claude Code stamps every content block within one assistant turn with the
 * same top-level entry `uuid` — a turn with two `tool_use` blocks (parallel
 * tool calls) produces two ToolEvents that share that `uuid` but carry
 * distinct `toolUseId`s (see `transcript_chat.py`'s module docstring).
 * Deduping tool events on `uuid` alone would collapse the second call into
 * the first and silently drop it. `toolUseId` is the part that's actually
 * unique per tool call, and it stays stable when the live tailer republishes
 * the same event (mutated in place) once its `tool_result` lands — which is
 * exactly the "same uuid REPLACES" case this needs to resolve correctly.
 */
function eventKey(ev: TimelineChatEvent): string {
  if (ev.kind === "tool" && ev.toolUseId) return `tool:${ev.toolUseId}`;
  /* Die ART gehoert in den Schluessel, nicht nur die uuid: EIN Transkript-
     Eintrag kann MEHRERE Bloecke tragen (Denken und Antwort in derselben
     Zeile), und alle erben dieselbe Eintrags-uuid. Ohne die Art galt die
     Antwort als Dublette des Denkens und wurde verworfen.
     Live gemessen an Sparkys omp-Transkript (21.08.2026): 10 von 25
     Eintraegen betroffen — Sparky dachte im Chat sichtbar nach und sagte
     nie etwas. Claude Code schreibt je Block eine eigene Zeile, deshalb
     fiel es bis zum ersten fremden Adapter nicht auf. */
  return `${ev.kind}:${ev.uuid}`;
}

function reindex(events: TimelineChatEvent[]): Map<string, number> {
  const index = new Map<string, number>();
  events.forEach((ev, i) => index.set(eventKey(ev), i));
  return index;
}

function pushOrReplace(state: ChatReducerState, ev: TimelineChatEvent): ChatReducerState {
  const key = eventKey(ev);
  const existingIndex = state.index.get(key);

  if (existingIndex !== undefined) {
    if (ev.kind !== "tool") {
      // Non-tool duplicate (Claude Code can repeat a line verbatim across a
      // resumed session) — ignored, first write wins.
      return state;
    }
    const events = state.events.slice();
    events[existingIndex] = ev;
    return { ...state, events };
  }

  let events = state.events.concat(ev);
  let index: Map<string, number>;
  if (events.length > MAX_CHAT_EVENTS) {
    events = events.slice(events.length - MAX_CHAT_EVENTS);
    index = reindex(events);
  } else {
    index = new Map(state.index);
    index.set(key, events.length - 1);
  }
  return { ...state, events, index };
}

export function chatReducer(state: ChatReducerState, event: ChatEvent): ChatReducerState {
  switch (event.kind) {
    case "state":
      return { ...state, state: event };
    case "usage":
      return { ...state, usage: event };
    case "session_changed":
      /* Der VERBRAUCH gehoerte zum alten Gespraech und wird geleert: nach
         /clear stand der Kontext-Ring sonst weiter auf dem alten Prozentwert,
         bis der Agent das naechste Mal antwortete — bei einem ruhenden
         Agenten beliebig lange. Nichts zeigen ist richtig, ein falscher Wert
         nicht.

         Der ZUSTAND bleibt: er beschreibt den Agenten und sein Terminal, nicht
         das Transkript. Ein Rollover wechselt die Datei, nicht den Agenten —
         ihn hier zu leeren erzeugte nur ein Flackern nach "Status unklar". */
      return {
        ...state,
        events: [],
        index: new Map(),
        usage: null,
        sessionChangedAt: state.sessionChangedAt + 1,
      };
    case "message":
    case "tool":
    case "thinking":
    case "command":
    case "notification":
      return pushOrReplace(state, event);
    default:
      return state;
  }
}

/**
 * Die Reihenfolge, in der Historie und bereits eingetroffene Live-Ereignisse
 * in den Reducer gehen.
 *
 * Beide Quellen laufen gleichzeitig an: die Historie ist ein HTTP-Rundlauf,
 * der Live-Strom eine offene Verbindung. Bei einem ARBEITENDEN Agenten
 * schreibt das Transkript im Sekundentakt — trifft eine Live-Zeile ein,
 * bevor die Historie da ist, steht sie allein in der Liste, und die Historie
 * wird danach hinten angehaengt. Der Reducer haengt nur an und sortiert nie
 * (bewusst: Transkript-Zeilen kommen in Schreibreihenfolge). Sichtbar wird
 * das als die neueste Zeile GANZ OBEN, das ganze Gespraech darunter — und
 * der Sprung ans Ende landet dann auf der aeltesten statt der neuesten
 * Nachricht.
 *
 * Darum sammelt der Hook Live-Ereignisse, solange die Historie der aktuellen
 * Sitzung noch nicht eingespielt ist, und gibt sie erst DANACH weiter. Das
 * ist auch dann richtig, wenn eine Zeile in beiden Quellen vorkommt (der
 * Tailer setzt beim Verbinden ans Dateiende auf, die Historie liest bis zum
 * Lesezeitpunkt): der Reducer verwirft Doppelte anhand des Schluessels.
 */
export function seedSequence(
  history: ChatEvent[],
  buffered: ChatEvent[],
): ChatEvent[] {
  return [...history, ...buffered];
}

/** Abstand zwischen "die CLI hat den Befehl angenommen" und "sie hat ihn
 *  angewendet und die Datei geschrieben". Grosszuegig gewaehlt: ein zu
 *  frueher zweiter Abruf kostet nur einen Rundlauf, ein zu spaeter laesst den
 *  Chip laenger falsch stehen. */
const MODEL_SETTLE_MS = 2000;

/** Eine einzige leere Liste statt eines frischen `[]` je Rendern — sonst
 *  wechselt die Kennung bei jedem Durchlauf und jedes `useMemo` darauf
 *  rechnet umsonst neu. */
const EMPTY_RUNS: SubagentRun[] = [];

// ── Hook ─────────────────────────────────────────────────────────────────────

export interface UseChatStreamResult {
  events: TimelineChatEvent[];
  /** Die Subagenten-Laeufe DIESER Sitzung — Handshake-Wert aus dem
   *  History-Abruf, kein Live-Wert (siehe `read_history` im Backend). Leer
   *  bei aelteren Backends und bei jedem Harness ohne eigenes Layout. */
  subagentRuns: SubagentRun[];
  state: StateEvent | null;
  usage: UsageEvent | null;
  session: ChatSession | null;
  hasMore: boolean;
  /** True once at least one frame (history load or live SSE event) has been
   *  observed since the last (re)connect attempt. There's no `onopen` signal
   *  wired into `useSSE` today, so this is an activity-based approximation,
   *  not a raw EventSource.readyState mirror. */
  connected: boolean;
  loading: boolean;
  error: Error | null;
  /** Server-derived harness capabilities (effort levels). `null` while history
   *  is still loading, or on a backend that predates the field. */
  capabilities: ChatCapabilities | null;
  /** Locally-rendered messages awaiting their transcript confirmation, oldest
   *  first. Render these AFTER `events`. */
  pendingEchoes: PendingEcho[];
  /** Call the moment a send is dispatched — before the request resolves. */
  echoSent: (text: string) => void;
  /** Call when that send failed: the echo is removed again, since nothing was
   *  delivered and a lingering bubble would claim otherwise. */
  echoFailed: (text: string) => void;
  /** Call on a 409 `agent_starting`: the echo waits calmly and the send is
   *  retried once via the supplied function. */
  echoAgentStarting: (text: string, retry: () => void) => void;
  /** True from a send until the transcript shows any sign of life (a state
   *  change, a tool call, a message). Drives the honest "Gesendet…" line. */
  awaitingResponse: boolean;
}

/**
 * Loads an agent's chat history (TanStack Query) and tails its live
 * transcript (`useSSE` on the `chat_event` channel), reducing both into one
 * timeline via `chatReducer`. A `session_changed` frame — live tail rolled
 * onto a new transcript file — clears the timeline and triggers a history
 * refetch so the new session's own history re-seeds it, rather than trying
 * to reconcile two unrelated transcripts' events.
 */
export function useChatStream(agentId: string | null, enabled = true): UseChatStreamResult {
  const [chatState, dispatch] = useReducer(chatReducer, undefined, createInitialChatState);
  const [connected, setConnected] = useState(false);
  const [pendingEchoes, setPendingEchoes] = useState<PendingEcho[]>([]);
  const [awaitingResponse, setAwaitingResponse] = useState(false);
  const echoSeqRef = useRef(0);
  /* Welche Sitzung zuletzt eingespeist wurde — Grundlage dafuer, einen
     Sitzungswechsel zu erkennen, den der Live-Strom verpasst hat. */
  const seededSessionIdRef = useRef<string | null>(null);
  /* WANN zuletzt eingespeist wurde. Frueher haing der Waechter an der
     Sitzungs-ID: eine frische Antwort mit DERSELBEN ID wurde verworfen.
     Genau die ist aber der einzige Lueckenfueller, wenn der Strom weg war
     (iOS killt SSE im Hintergrund; der Tailer laeuft mit fortlaufendem
     Offset weiter, die Ereignisse dieser Zeit erreichen den Client nie).
     Sichtbar als: Handy sperren, entsperren, und der Chat steht fuer immer
     auf dem alten Stand. `pushOrReplace` ist fuer bekannte Schluessel
     idempotent, erneutes Einspeisen kostet also nur Arbeit, nie Richtigkeit. */
  const seededAtRef = useRef(0);
  const hasSeededRef = useRef(false);
  /* Live-Zeilen, die vor der ersten Historien-Antwort eintreffen — siehe
     `seedSequence`. */
  const liveBufferRef = useRef<TimelineChatEvent[]>([]);
  /* Laufende Wiederhol-Timer (agent_starting), damit sie beim Verlassen
     sterben statt an den vorigen Agenten zu senden. */
  const retryTimersRef = useRef<Set<number>>(new Set());

  useEffect(() => {
    const timers = retryTimersRef.current;
    return () => {
      timers.forEach((h) => window.clearTimeout(h));
      timers.clear();
    };
  }, []);

  const queryEnabled = enabled && !!agentId;

  const historyQuery = useQuery({
    queryKey: ["chat-history", agentId],
    queryFn: () => api.chat.history(agentId as string),
    enabled: queryEnabled,
  });

  useEffect(() => {
    const data = historyQuery.data;
    if (!data) return;
    if (seededAtRef.current === historyQuery.dataUpdatedAt) return;
    seededAtRef.current = historyQuery.dataUpdatedAt;

    /* Ein Sitzungswechsel, den der Live-Strom nicht mitbekommen hat (er lag
       waehrenddessen tot — Hintergrund-Tab, gesperrtes Handy). Ohne dieses
       Leeren stuenden zwei fremde Gespraeche ohne Trenner untereinander, als
       waere es eines. */
    if (hasSeededRef.current && seededSessionIdRef.current !== data.session.sessionId) {
      dispatch({ kind: "session_changed" });
    }
    seededSessionIdRef.current = data.session.sessionId;
    hasSeededRef.current = true;

    const buffered = liveBufferRef.current;
    liveBufferRef.current = [];
    for (const ev of seedSequence(data.events, buffered)) dispatch(ev);
  }, [historyQuery.data, historyQuery.dataUpdatedAt]);

  /* Faellt der Historien-Abruf aus, duerfen die gepufferten Live-Zeilen nicht
     im Puffer verhungern — sonst waere aus einer falschen Reihenfolge ein
     leerer Chat geworden. Lieber unsortiert zeigen als gar nichts. */
  useEffect(() => {
    if (!historyQuery.isError || hasSeededRef.current) return;
    hasSeededRef.current = true;
    const buffered = liveBufferRef.current;
    liveBufferRef.current = [];
    for (const ev of buffered) dispatch(ev);
  }, [historyQuery.isError]);

  const echoSent = useCallback((text: string) => {
    echoSeqRef.current += 1;
    setPendingEchoes((prev) => [
      ...prev,
      { id: `echo-${echoSeqRef.current}`, text, sentAt: Date.now(), status: "pending" },
    ]);
    setAwaitingResponse(true);
  }, []);

  const echoFailed = useCallback((text: string) => {
    setPendingEchoes((prev) => withdrawPendingEcho(prev, text));
    setAwaitingResponse(false);
  }, []);

  /**
   * The backend refused because the agent is still booting. That is a wait, not
   * a failure: the echo says so calmly and the send is retried ONCE, after a
   * delay. One retry rather than a loop — if the agent still isn't up by then,
   * that is news the operator should get instead of an indefinitely spinning
   * bubble. `retry` is the caller's own send function, so the retry travels the
   * exact same path (including its error handling) as the original.
   */
  const echoAgentStarting = useCallback((text: string, retry: () => void) => {
    let alreadyRetried = false;
    setPendingEchoes((prev) => {
      const target = [...prev].reverse().find((e) => sameMessage(e.text, text));
      alreadyRetried = target?.retried === true;
      return alreadyRetried ? prev : markEchoStarting(prev, text);
    });
    if (alreadyRetried) return;
    /* Der Handle wird gemerkt und beim Verlassen geloescht. Ohne das lief der
       Timer nach einem Agentenwechsel weiter (ChatView ist auf die Agenten-ID
       gekeyt, montiert also neu) und schickte 10 s spaeter doch noch an den
       VORIGEN Agenten — samt Fehler-Toast ohne sichtbaren Bezug. */
    const handle = window.setTimeout(() => {
      retryTimersRef.current.delete(handle);
      setPendingEchoes((prev) => markEchoRetried(prev, text));
      retry();
    }, ECHO_RETRY_DELAY_MS);
    retryTimersRef.current.add(handle);
  }, []);

  /** Retires the echo this transcript message corresponds to. Prefers a text
   *  match; falls back to the oldest echo, because a visible duplicate bubble is
   *  a worse failure than retiring one echo early (the real message is on screen
   *  either way — only the local copy disappears). */
  /** `fromHistory` = this text came from a bulk history page, not a live event,
   *  so no oldest-echo fallback (see reconcilePendingEchoes). */
  const reconcileEcho = useCallback((incomingText: string, fromHistory = false) => {
    setPendingEchoes((prev) =>
      reconcilePendingEchoes(prev, incomingText, { allowOldestFallback: !fromHistory }),
    );
  }, []);

  // Retire echoes that history brings in (a refetch after session rollover can
  // carry a message that was still pending locally).
  useEffect(() => {
    const data = historyQuery.data;
    if (!data) return;
    for (const ev of data.events) {
      if (ev.kind === "message" && ev.role === "user") reconcileEcho(ev.text, true);
      // Ein getippter Slash-Befehl kommt als `command` zurueck, nicht als
      // Nachricht — sein Echo wurde darum nie abgeraeumt und stand nach 10 s
      // dauerhaft als "Nicht bestaetigt" da. Betrifft auch die Modell-Auswahl
      // im Dropdown: sie sendet `/model <name>` durch denselben Weg.
      if (ev.kind === "command") reconcileEcho(ev.command, true);
    }
  }, [historyQuery.data, reconcileEcho]);

  // Flip long-unacknowledged echoes rather than leaving them looking delivered —
  // but the clock only runs while the agent is NOT mid-turn (see
  // markUnconfirmedEchoes: the CLI queues messages sent during a turn, so a
  // timer running then accuses a message that is perfectly safe).
  // Only while the stream is healthy: on a dead stream we genuinely don't know,
  // and StatusLine already says the status is unclear.
  const agentWorking = chatState.state?.status === "working";
  useEffect(() => {
    if (pendingEchoes.length === 0 || !connected) return;
    const timer = setInterval(() => {
      const now = Date.now();
      setPendingEchoes((prev) => markUnconfirmedEchoes(prev, now, agentWorking));
    }, 1_000);
    return () => clearInterval(timer);
  }, [pendingEchoes.length, connected, agentWorking]);

  const streamUrl = agentId ? api.chat.streamUrl(agentId) : "";

  const onSSEEvent = useCallback(
    (eventName: string, data: Record<string, unknown>) => {
      /* Der Keepalive ist der EINZIGE Verkehr auf einem ruhenden Agenten.
         Ohne ihn blieb `connected` false, bis zufaellig etwas passierte —
         und die Statuszeile behauptete solange "Status unklar". Er traegt
         keine Nutzlast, beruehrt also den Reducer nicht. */
      if (eventName === "ping") { setConnected(true); return; }
      if (eventName !== "chat_event") return;
      setConnected(true);
      const ev = data as unknown as ChatEvent;
      /* Zeitachsen-Zeilen warten, solange die Historie der aktuellen Sitzung
         noch nicht eingespeist ist — sonst stuende die neueste Zeile ueber
         dem ganzen Gespraech (siehe `seedSequence`). Zustand, Verbrauch und
         der Sitzungswechsel selbst sind keine Zeitachsen-Zeilen und gelten
         sofort: sie ordnen nichts ein, sie ersetzen ein Anzeigefach. */
      const isTimeline =
        ev.kind === "message" || ev.kind === "tool" ||
        ev.kind === "thinking" || ev.kind === "command" ||
        ev.kind === "notification";
      if (isTimeline && !hasSeededRef.current) {
        liveBufferRef.current.push(ev);
      } else {
        dispatch(ev);
      }
      if (ev.kind === "message" && ev.role === "user") reconcileEcho(ev.text);
      if (ev.kind === "command") {
        reconcileEcho(ev.command);
        /* Ein Modellwechsel aendert die `settings.json` des Agenten — und
           genau daraus liest `capabilities.model` den Wert fuer den Chip.
           Ohne erneutes Lesen zeigte er weiter das alte Modell, bis zufaellig
           etwas anderes einen Abruf ausloeste; bei einem ruhenden Agenten
           also beliebig lange (Operator-Befund 22.08.2026).

           Zweimal gelesen, nicht geraten: sofort, und einmal nach einer
           kurzen Weile. Der Befehl steht im Transkript, sobald die CLI die
           EINGABE angenommen hat — die Datei schreibt sie erst, wenn sie den
           Wechsel ANWENDET. Der zweite Abruf deckt diesen Abstand ab.
           Angezeigt wird immer nur, was gelesen wurde: bleibt die CLI
           laenger haengen, steht kurz der alte Wert da — nie ein erfundener
           neuer. */
        if (/^\/model\b/.test(ev.command)) {
          historyQuery.refetch();
          const handle = window.setTimeout(() => {
            retryTimersRef.current.delete(handle);
            historyQuery.refetch();
          }, MODEL_SETTLE_MS);
          retryTimersRef.current.add(handle);
        }
      }
      // Ein Sitzungswechsel beantwortet genau die Kommandos, die ihn ausloesen
      // (/clear) — ihre Bestaetigung kaeme sonst nie, weil das alte Transkript
      // weg ist und das neue leer startet.
      if (ev.kind === "session_changed") {
        setPendingEchoes((prev) => retireEchoesAnsweredByRollover(prev));
      }
      // Any sign the agent processed the turn ends the "Gesendet…" line. A
      // `usage` frame alone doesn't count — it can arrive for the previous turn.
      if (ev.kind === "state" || ev.kind === "tool" || ev.kind === "thinking" || ev.kind === "message") {
        setAwaitingResponse(false);
      }
      if (ev.kind === "session_changed") {
        // The new session has different history — force a re-seed once the
        // refetch resolves instead of trusting the (now stale) sessionId.
        seededSessionIdRef.current = null;
        hasSeededRef.current = false;
        liveBufferRef.current = [];
        historyQuery.refetch();
      }
    },
    [historyQuery, reconcileEcho],
  );

  const onSSEError = useCallback(() => setConnected(false), []);

  useSSE(streamUrl, {
    enabled: queryEnabled && !!streamUrl,
    onEvent: onSSEEvent,
    onError: onSSEError,
  });

  return {
    events: chatState.events,
    subagentRuns: historyQuery.data?.subagentRuns ?? EMPTY_RUNS,
    state: chatState.state,
    usage: chatState.usage,
    session: historyQuery.data?.session ?? null,
    hasMore: historyQuery.data?.hasMore ?? false,
    connected,
    loading: historyQuery.isLoading,
    error: historyQuery.error as Error | null,
    capabilities: historyQuery.data?.capabilities ?? null,
    pendingEchoes,
    echoSent,
    echoFailed,
    echoAgentStarting,
    awaitingResponse,
  };
}
