"use client";

/**
 * The app-wide live voice call (voice PR 5).
 *
 * Until PR 4 a call lived inside one chat and ended when the operator left
 * it. Operator rule 12.10.2026: the mic in a chat talks to that chat's agent,
 * the mic in the navigation talks to the LEAD agent, and a call survives
 * moving between pages. So the call moved up here, ABOVE the pages:
 * `VoiceCallRoot` sits in the root `Providers` (app/providers.tsx). It must
 * not live in AppShell: every page renders its own AppShell, so a client
 * navigation remounts it — and a provider there ended the call on every
 * route change (live finding 13.10.2026, PR #808). AppShell only renders the
 * floating bar (`FloatingVoiceCall`), which reads this provider.
 *
 * - One call at a time, app-wide. `startCall(target)` opens it for the given
 *   agent (the chat's agent, or the lead agent from the navigation mic).
 * - A chat (`VoiceLiveDock`) that is on screen registers itself. When the
 *   call's agent has its chat open, the call DOCKS there: the full call bar
 *   above the composer, the chat's own stream for listening along, the echo
 *   of forwarded messages. Any other chat shows the compact bar in-flow.
 * - With no chat on screen, the compact bar floats: in-flow above the phone
 *   tab bar, as a small overlay card bottom-right on desktop. The voice keeps
 *   listening along through a stream of its own (`CallStreamListener`).
 * - The lead agent comes from MC (`GET /voice/live/default-target`) — the
 *   public repo never names it.
 *
 * Jarvis (LiveKit, `VoiceWidget.tsx`) is untouched; it only moved from the
 * navigation mic into the menus (QuickSheet, command palette).
 *
 * Cost (voice PR 6): when the daily budget is used up, every mic is greyed
 * and a tap starts no call and asks for no microphone — it shows "Tagesbudget
 * erreicht — wieder ab <Uhrzeit>" where call errors show (live finding
 * 14.10.). The config is fetched again after each call and at the reset
 * time, so the mics learn both without a reload. Both bars show the call's
 * elapsed time and the idle warning.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useLocale, useTranslations } from "next-intl";
import { PhoneOff, RotateCcw, X } from "lucide-react";
import { api, getToken } from "@/lib/api";
import { useAppStore } from "@/lib/store";
import { C, STATUS_TEXT } from "@/lib/colors";
import { chatEntryHref } from "@/lib/chatHistory";
import { useChatStream } from "@/hooks/useChatStream";
import {
  ERROR_MESSAGE_KEY,
  budgetBlocks,
  formatElapsed,
  formatResetTime,
  isRetryable,
  type VoiceLiveStatus,
} from "./voiceLiveCore";
import { browserVoiceLiveEnv, useVoiceLive, type VoiceLiveCall, type VoiceLiveEnv } from "./useVoiceLive";
import { refreshVoiceLiveConfig, useVoiceLiveConfig } from "./useVoiceLiveConfig";
import type { ProgressSnapshot } from "./progress";
import { setVoiceLiveActive } from "./voiceLiveActivity";

export interface VoiceTarget {
  agentId: string;
  agentName: string;
}

type ForwardFn = (text: string, midTurn: boolean) => void;
type StoppedFn = (takenBack: string[] | null) => void;

/** What a chat on screen hands the call: the echo of forwarded messages and
 *  the follow-ups a voice stop took back (voice PR 4), both for its composer. */
export interface DockHandlers {
  onForwarded?: ForwardFn;
  onStopped?: StoppedFn;
}

export interface VoiceLiveCallContextValue {
  /** The backend has the live voice on (`VOICE_LIVE_ENABLED` + key). */
  enabled: boolean;
  /** Today's voice budget is used up: the mics are greyed and a tap starts
   *  no call, it only explains (voice PR 6). */
  budgetReached: boolean;
  /** When the budget frees up again (ISO, from MC); null when unknown. */
  budgetResetsAt: string | null;
  call: VoiceLiveCall;
  /** A call is open, connecting or shows its error. */
  active: boolean;
  /** Who the open (or last) call talks to. */
  target: VoiceTarget | null;
  /** The navigation mic's agent: the lead agent, null while unknown/none. */
  lead: VoiceTarget | null;
  startCall: (target: VoiceTarget) => void;
  /** A chat for `agentId` is on screen; returns the unregister. */
  registerDock: (agentId: string, handlers: DockHandlers) => () => void;
  /** The docked chat's live stream, for the voice to listen along. */
  feedProgress: (agentId: string, snap: ProgressSnapshot | undefined) => void;
  /** Agent ids whose chat is on screen right now. */
  docks: string[];
}

const VoiceLiveCallContext = createContext<VoiceLiveCallContextValue | null>(null);

/** null outside the provider (tests of single components, login page). */
export function useVoiceLiveCall(): VoiceLiveCallContextValue | null {
  return useContext(VoiceLiveCallContext);
}

// One lookup per board per page load; a failure means "no lead" (Jarvis).
const leadCache = new Map<string, Promise<VoiceTarget | null>>();

export function loadLeadTarget(boardId: string | null): Promise<VoiceTarget | null> {
  const key = boardId ?? "";
  let hit = leadCache.get(key);
  if (!hit) {
    hit = Promise.resolve()
      .then(() => api.voiceLive.defaultTarget(boardId))
      .then((r) => (r?.agent_id ? { agentId: r.agent_id, agentName: r.agent_name } : null))
      .catch(() => null);
    leadCache.set(key, hit);
  }
  return hit;
}

/** Test seam. */
export function resetLeadTargetCache(): void {
  leadCache.clear();
}

function useLeadTarget(active: boolean): VoiceTarget | null {
  const boardId = useAppStore().activeBoardId ?? null;
  const [lead, setLead] = useState<VoiceTarget | null>(null);
  useEffect(() => {
    if (!active) return;
    let alive = true;
    void loadLeadTarget(boardId).then((t) => {
      if (alive) setLead(t);
    });
    return () => {
      alive = false;
    };
  }, [active, boardId]);
  return active ? lead : null;
}

/** Only an empty snapshot (a stream still loading) is not worth looking at:
 *  it would read as "the agent stopped" to the progress tracker. */
function loaded(snap: ProgressSnapshot | undefined): snap is ProgressSnapshot {
  return !!snap && (snap.state !== null || snap.events.length > 0 || snap.todos !== null);
}

/** Listening along while no chat of the call's agent is on screen. */
function CallStreamListener({
  agentId,
  onSnapshot,
}: {
  agentId: string;
  onSnapshot: (snap: ProgressSnapshot) => void;
}) {
  const stream = useChatStream(agentId, true);
  const snap = useMemo(
    () => ({ state: stream.state, events: stream.events, todos: stream.todos }),
    [stream.state, stream.events, stream.todos],
  );
  const loading = stream.loading;
  useEffect(() => {
    if (!loading && loaded(snap)) onSnapshot(snap);
  }, [snap, loading, onSnapshot]);
  return null;
}

/**
 * The one app-wide call, mounted once in the root providers. Only switched
 * on while someone is signed in: on /login a config request would answer
 * 401 and bounce back to /login. Re-checked on every route change (login →
 * app is a client navigation); signing out hangs up.
 */
export function VoiceCallRoot({ children, env }: { children: ReactNode; env?: VoiceLiveEnv }) {
  const pathname = usePathname();
  const [signedIn, setSignedIn] = useState(false);
  useEffect(() => setSignedIn(getToken() !== ""), [pathname]);
  return (
    <VoiceLiveCallProvider env={env} active={signedIn}>
      {children}
    </VoiceLiveCallProvider>
  );
}

export function VoiceLiveCallProvider({
  children,
  env = browserVoiceLiveEnv,
  resolveLead = true,
  active: on = true,
}: {
  children: ReactNode;
  env?: VoiceLiveEnv;
  /** false = a chat on its own (no AppShell): no navigation mic to feed. */
  resolveLead?: boolean;
  /** false = nobody signed in: no lookups, no call. */
  active?: boolean;
}) {
  const locale = useLocale();
  const config = useVoiceLiveConfig(on);
  const enabled = config !== null;
  const budget = config?.budget ?? null;
  // Re-render at the reset time: the mics open again without a reload.
  const [, setResetTick] = useState(0);
  const budgetReached = budgetBlocks(budget, Date.now());
  const budgetResetsAt = budget?.resets_at ?? null;
  useEffect(() => {
    if (!budgetReached || !budgetResetsAt) return;
    const wait = Math.min(Math.max(Date.parse(budgetResetsAt) - Date.now(), 0) + 1000, 2_147_483_647);
    const id = setTimeout(() => {
      setResetTick((n) => n + 1);
      void refreshVoiceLiveConfig();
    }, wait);
    return () => clearTimeout(id);
  }, [budgetReached, budgetResetsAt]);
  const budgetRef = useRef(budget);
  budgetRef.current = budget;
  const isBudgetReached = useCallback(() => budgetBlocks(budgetRef.current, Date.now()), []);
  const lead = useLeadTarget(enabled && resolveLead);
  const [target, setTarget] = useState<VoiceTarget | null>(null);
  const [progress, setProgress] = useState<ProgressSnapshot | undefined>(undefined);
  const [docks, setDocks] = useState<string[]>([]);
  const docksRef = useRef(new Map<string, DockHandlers>());
  const targetRef = useRef<VoiceTarget | null>(null);

  // Only the docked chat gets these; with the chat closed, it reloads its
  // history when opened, so nothing is lost.
  const onForwarded = useCallback((text: string, midTurn: boolean) => {
    const id = targetRef.current?.agentId;
    if (id) docksRef.current.get(id)?.onForwarded?.(text, midTurn);
  }, []);
  const onStopped = useCallback((takenBack: string[] | null) => {
    const id = targetRef.current?.agentId;
    if (id) docksRef.current.get(id)?.onStopped?.(takenBack);
  }, []);

  // The hook's own agentId stays fixed (""): the agent is picked per call via
  // start(id), so a page change never "switches agent" and ends the call.
  const call = useVoiceLive("", locale === "de" ? "de" : "en", env, {
    onForwarded,
    onStopped,
    progress,
    isBudgetReached,
  });
  const active = call.status !== "idle";
  const hangUpFn = call.hangUp;
  useEffect(() => {
    if (!on) hangUpFn();
  }, [on, hangUpFn]);

  const startFn = call.start;
  const startCall = useCallback(
    (t: VoiceTarget) => {
      targetRef.current = t;
      setTarget(t);
      setProgress(undefined);
      // Straight from the tap: iOS only lets audio play that a gesture began.
      void startFn(t.agentId);
    },
    [startFn],
  );

  const registerDock = useCallback((agentId: string, handlers: DockHandlers) => {
    docksRef.current.set(agentId, handlers);
    setDocks((d) => [...d, agentId]);
    return () => {
      if (docksRef.current.get(agentId) === handlers) docksRef.current.delete(agentId);
      setDocks((d) => {
        const i = d.indexOf(agentId);
        return i < 0 ? d : [...d.slice(0, i), ...d.slice(i + 1)];
      });
    };
  }, []);

  const callAgentId = active ? target?.agentId ?? null : null;
  const callAgentRef = useRef(callAgentId);
  callAgentRef.current = callAgentId;
  const feedProgress = useCallback((agentId: string, snap: ProgressSnapshot | undefined) => {
    if (agentId === callAgentRef.current && loaded(snap)) setProgress(snap);
  }, []);
  const onListenerSnapshot = useCallback((snap: ProgressSnapshot) => setProgress(snap), []);

  // The chat header hides its Jarvis mic while this agent is on a call.
  useEffect(() => {
    if (!callAgentId) return;
    setVoiceLiveActive(callAgentId, true);
    return () => setVoiceLiveActive(callAgentId, false);
  }, [callAgentId]);

  // A call cost money (or was refused for the budget): ask MC again, so
  // every mic shows "budget reached" without a reload.
  const wasActive = useRef(false);
  useEffect(() => {
    if (!enabled) return;
    const ended = wasActive.current && (!active || call.status === "error");
    wasActive.current = active && call.status !== "error";
    if (ended) void refreshVoiceLiveConfig();
  }, [enabled, active, call.status]);

  const docked = callAgentId !== null && docks.includes(callAgentId);

  const value = useMemo<VoiceLiveCallContextValue>(
    () => ({
      enabled,
      budgetReached,
      budgetResetsAt,
      call,
      active,
      target,
      lead,
      startCall,
      registerDock,
      feedProgress,
      docks,
    }),
    [enabled, budgetReached, budgetResetsAt, call, active, target, lead, startCall, registerDock, feedProgress, docks],
  );

  return (
    <VoiceLiveCallContext.Provider value={value}>
      {children}
      {enabled && callAgentId && !docked && (
        <CallStreamListener key={callAgentId} agentId={callAgentId} onSnapshot={onListenerSnapshot} />
      )}
    </VoiceLiveCallContext.Provider>
  );
}

const STATE_KEY: Record<Exclude<VoiceLiveStatus, "idle">, string> = {
  connecting: "stateConnecting",
  listening: "stateListening",
  speaking: "stateSpeaking",
  error: "stateEnded",
};

/** The call's elapsed time (`m:ss`), ticking once a second while connected. */
export function CallClock({ call }: { call: VoiceLiveCall }) {
  const since = call.connectedAt;
  const now = call.now;
  const [elapsed, setElapsed] = useState(() => (since === null ? 0 : now() - since));
  const live = since !== null && call.status !== "error" && call.status !== "idle";
  useEffect(() => {
    if (since === null) return;
    setElapsed(now() - since);
    if (!live) return;
    const id = setInterval(() => setElapsed(now() - since), 1000);
    return () => clearInterval(id);
  }, [since, now, live]);
  if (since === null) return null;
  return (
    <span
      data-testid="voice-elapsed"
      className="shrink-0 font-mono text-xs tabular-nums"
      style={{ color: C.textMuted }}
    >
      {formatElapsed(elapsed)}
    </span>
  );
}

/** i18n key + values of an error, with the call's own limits filled in.
 *  `resetTime` = when the daily budget frees up (local "HH:MM"). Numbers go
 *  in as numbers: the texts pick "1 minute" / "2 minutes" (ICU plural). */
export function errorText(
  t: (key: string, values?: Record<string, string | number>) => string,
  call: VoiceLiveCall,
  resetTime: string | null = null,
): string {
  if (call.error === "budget_reached" && resetTime) return t("errBudgetReachedUntil", { time: resetTime });
  return t(ERROR_MESSAGE_KEY[call.error!], {
    minutes: Math.max(1, Math.round(call.limits.max_call_s / 60)),
    seconds: call.limits.idle_hangup_s,
  });
}

/** The budget's reset as local time, for `errorText` (null outside a provider). */
export function useBudgetResetTime(): string | null {
  const ctx = useVoiceLiveCall();
  const locale = useLocale();
  return formatResetTime(ctx?.budgetResetsAt, locale);
}

/**
 * One row for a call whose chat is not on screen: who, what state, a way to
 * its chat, hang up. In-flow inside another chat; floating otherwise.
 */
export function VoiceCallCompact({
  call,
  target,
  showOpen = true,
}: {
  call: VoiceLiveCall;
  target: VoiceTarget;
  showOpen?: boolean;
}) {
  const t = useTranslations("voiceLive");
  const resetTime = useBudgetResetTime();
  if (call.status === "idle") return null;
  const isError = call.status === "error" && call.error !== null;
  // Short form: the compact row is narrow on a phone, the seconds must show.
  const stateKey =
    call.idleWarning && call.status !== "error"
      ? "stateIdleWarningShort"
      : call.muted && call.status !== "error"
      ? "stateMuted"
      : call.passing && call.status !== "error"
      ? "statePassing"
      : call.narrating && call.status === "speaking"
        ? "stateNarrating"
        : STATE_KEY[call.status as Exclude<VoiceLiveStatus, "idle">];

  return (
    <div
      data-testid="voice-call-compact"
      data-status={call.status}
      data-agent={target.agentId}
      className="flex items-center gap-2 pl-3 pr-1 py-1 rounded-xl"
      style={{ backgroundColor: C.bgSurface, border: `1px solid ${C.border}` }}
    >
      {isError ? (
        <>
          <div role="alert" className="flex-1 min-w-0 text-sm" style={{ color: C.textPrimary }}>
            {errorText(t, call, resetTime)}
          </div>
          {isRetryable(call.error!) && (
            <button
              type="button"
              onClick={() => void call.start(target.agentId)}
              aria-label={t("retry")}
              title={t("retry")}
              className="shrink-0 inline-flex items-center justify-center w-11 h-11 md:w-8 md:h-8 rounded-full cursor-pointer hover:bg-[var(--color-bg-hover)]"
              style={{ color: C.textSecondary }}
            >
              <RotateCcw size={15} />
            </button>
          )}
          <button
            type="button"
            onClick={call.dismiss}
            aria-label={t("close")}
            title={t("close")}
            data-testid="voice-compact-dismiss"
            className="shrink-0 inline-flex items-center justify-center w-11 h-11 md:w-8 md:h-8 rounded-full cursor-pointer hover:bg-[var(--color-bg-hover)]"
            style={{ color: C.textSecondary }}
          >
            <X size={16} />
          </button>
        </>
      ) : (
        <>
          <span
            aria-hidden
            className={`shrink-0 inline-block w-2 h-2 rounded-full ${call.status === "speaking" && !call.muted ? "motion-safe:animate-pulse" : ""}`}
            style={{ backgroundColor: call.status === "connecting" || call.muted ? C.textDim : C.accent }}
          />
          <span className="flex-1 min-w-0 truncate text-sm" aria-live="polite">
            <span style={{ color: C.textPrimary, fontWeight: 500 }}>{target.agentName}</span>
            <span style={{ color: C.textMuted }}> · </span>
            <span
              data-testid="voice-compact-state"
              style={{ color: call.idleWarning ? STATUS_TEXT.warning : C.textSecondary }}
            >
              {t(stateKey, { seconds: String(call.limits.idle_warn_s) })}
            </span>
          </span>
          <CallClock call={call} />
          {showOpen && (
            <Link
              href={chatEntryHref("agent", target.agentId)}
              data-testid="voice-compact-open"
              className="shrink-0 inline-flex items-center h-11 md:h-8 px-2 rounded-lg text-sm cursor-pointer hover:bg-[var(--color-bg-hover)]"
              style={{ color: C.textSecondary }}
            >
              {t("openChat")}
            </Link>
          )}
          <button
            type="button"
            onClick={call.hangUp}
            aria-label={t("hangUpWith", { name: target.agentName })}
            title={t("hangUp")}
            data-testid="voice-compact-hangup"
            className="shrink-0 inline-flex items-center justify-center w-11 h-11 md:w-8 md:h-8 rounded-full cursor-pointer"
            style={{ backgroundColor: C.error, color: C.onStatus }}
          >
            <PhoneOff size={15} />
          </button>
        </>
      )}
    </div>
  );
}

/**
 * The call while no chat is on screen (AppShell, between the page and the
 * phone tab bar). Phone: in the flow above the tab bar, so it never covers
 * content. Desktop: a small card bottom-right over the page (an overlay, so
 * the elevated shadow — DESIGN "Elevation").
 */
export function FloatingVoiceCall() {
  const ctx = useVoiceLiveCall();
  if (!ctx || !ctx.enabled || !ctx.active || !ctx.target) return null;
  if (ctx.docks.length > 0) return null;
  return (
    <div
      data-region="voice-call-floating"
      data-testid="voice-call-floating"
      className="shrink-0 px-3 pb-2 md:p-0 md:absolute md:bottom-4 md:right-6 md:z-30 md:w-96"
    >
      {/* Shadow on desktop only: on the phone the bar sits in the flow
          (DESIGN "Flach-Regel" — surfaces in the page flow carry none). */}
      <div className="rounded-xl md:shadow-elevated">
        <VoiceCallCompact call={ctx.call} target={ctx.target} />
      </div>
    </div>
  );
}
