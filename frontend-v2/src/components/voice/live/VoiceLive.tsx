"use client";

/**
 * Live voice in the chat — the visible part (voice PR 1, tools since PR 2).
 *
 * - `VoiceMicButton`: secondary icon button in the composer, left of send
 *   (DESIGN K11: send stays the one primary action). Greyed on plain http,
 *   but still tappable so the operator learns *why* (a disabled button on a
 *   phone explains nothing).
 * - `VoiceCallBar`: sits above the composer while a call runs or failed:
 *   live captions ("by voice" bubbles for the operator, a quiet line for the
 *   voice), one state word, hang-up. The composer stays usable below. What
 *   the voice passed on appears in the chat itself as the operator's message
 *   with a "by voice" label (ChatMessage); here the line gets a quiet tick.
 *   While the voice tells the agent's progress on its own (voice PR 3) the
 *   state word says so ("Erzählt Fortschritt") — nothing else changes.
 *   Mute (voice PR 4) is a symbol toggle next to hang-up: mic track off,
 *   call open, the state word says "Stumm" and the dot goes grey. The
 *   agent's own Stop stays the composer's button (one meaning per button).
 * - Cost and brakes (voice PR 6): the bar shows the call's elapsed time next
 *   to the state word. After a stretch of silence a notice ("Stille — Auflegen
 *   in 15 s") replaces the state word and a soft tone plays; any speech
 *   clears it. With today's budget used up the mic is greyed (crossed-out
 *   icon, aria-disabled) and a tap starts no call and asks for no
 *   microphone: the bar only says "Tagesbudget erreicht — wieder ab <Uhrzeit>"
 *   (live finding 14.10.: the title alone is invisible on a phone).
 */

import { type ReactNode, useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { Check, Mic, MicOff, PhoneOff, RotateCcw, X } from "lucide-react";
import { C, STATUS_TEXT } from "@/lib/colors";
import { isRetryable, type VoiceLiveStatus } from "./voiceLiveCore";
import { browserVoiceLiveEnv, type VoiceLiveCall, type VoiceLiveEnv } from "./useVoiceLive";
import type { ProgressSnapshot } from "./progress";
import {
  CallClock,
  VoiceCallCompact,
  errorText,
  useBudgetResetTime,
  VoiceLiveCallProvider,
  useVoiceLiveCall,
  type VoiceLiveCallContextValue,
} from "./VoiceLiveCall";

const STATE_KEY: Record<Exclude<VoiceLiveStatus, "idle">, string> = {
  connecting: "stateConnecting",
  listening: "stateListening",
  speaking: "stateSpeaking",
  error: "stateEnded",
};

export function VoiceMicButton({
  agentName,
  secure,
  budgetReached = false,
  onStart,
}: {
  agentName: string;
  secure: boolean;
  /** Today's voice budget is used up (voice PR 6). */
  budgetReached?: boolean;
  onStart: () => void;
}) {
  const t = useTranslations("voiceLive");
  const label = !secure ? t("micInsecure") : budgetReached ? t("micBudgetReached") : t("micStart", { name: agentName });
  const dim = !secure || budgetReached;
  // Not `disabled`: a disabled button on a phone explains nothing. The tap
  // stays, but the hook ends it at once with the budget notice (no call).
  return (
    <button
      type="button"
      onClick={onStart}
      aria-label={label}
      aria-disabled={budgetReached || undefined}
      title={label}
      data-testid="voice-mic-button"
      data-secure={secure}
      data-budget-reached={budgetReached}
      className={`shrink-0 inline-flex items-center justify-center w-11 h-11 md:w-8 md:h-8 rounded-full transition-colors ${
        budgetReached ? "cursor-not-allowed" : "cursor-pointer hover:bg-[var(--color-bg-hover)]"
      }`}
      style={{ color: dim ? C.textDim : C.textSecondary }}
    >
      {budgetReached ? <MicOff size={17} data-testid="voice-mic-off-icon" /> : <Mic size={17} />}
    </button>
  );
}

export function VoiceCallBar({ call, agentName }: { call: VoiceLiveCall; agentName: string }) {
  const t = useTranslations("voiceLive");
  const resetTime = useBudgetResetTime();
  const listRef = useRef<HTMLDivElement>(null);
  const lineCount = call.transcript.length;
  const lastText = call.transcript[lineCount - 1]?.text ?? "";

  // Pinned to the newest line. When older lines scroll out at the top they
  // fade instead of being cut through the middle of a word (PR 1 review:
  // "tun?" showed half a line).
  const [scrolled, setScrolled] = useState(false);
  useEffect(() => {
    const el = listRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
    setScrolled(el.scrollTop > 0);
  }, [lineCount, lastText]);

  if (call.status === "idle") return null;
  const isError = call.status === "error" && call.error !== null;
  const stateKey =
    call.idleWarning && call.status !== "error"
      ? "stateIdleWarning"
      : call.muted && call.status !== "error"
      ? "stateMuted"
      : call.passing && call.status !== "error"
      ? "statePassing"
      : call.narrating && call.status === "speaking"
        ? "stateNarrating"
        : STATE_KEY[call.status as Exclude<VoiceLiveStatus, "idle">];

  return (
    <div
      data-region="voice-call"
      data-testid="voice-call-bar"
      data-status={call.status}
      data-narrating={call.narrating}
      data-muted={call.muted}
      data-idle-warning={call.idleWarning}
      className="mx-3 mt-2 md:mx-4 rounded-xl"
      style={{ backgroundColor: C.bgSurface, border: `1px solid ${C.border}` }}
    >
      {lineCount > 0 ? (
        <div
          ref={listRef}
          data-testid="voice-transcript"
          data-scrolled={scrolled}
          aria-live="polite"
          onScroll={(e) => setScrolled(e.currentTarget.scrollTop > 0)}
          className={`max-h-40 overflow-y-auto overscroll-contain px-3 pt-3 pb-1 flex flex-col gap-2 ${
            scrolled ? "[mask-image:linear-gradient(to_bottom,transparent_1.25rem,black_3.5rem)]" : ""
          }`}
        >
          {call.transcript.map((line) =>
            line.who === "you" ? (
              <div
                key={`you-${line.id}`}
                data-testid="voice-line-you"
                data-forwarded={line.forwarded === true}
                className="self-end shrink-0 max-w-[85%] rounded-xl px-3 py-2"
                style={{ backgroundColor: C.accentSubtle }}
              >
                <div className="flex items-center gap-1 text-xs" style={{ color: C.textMuted }}>
                  {t("byVoice")}
                  {line.forwarded && (
                    <span className="inline-flex items-center gap-1" data-testid="voice-line-forwarded">
                      · <Check size={12} aria-hidden="true" />
                      {t("forwarded", { name: agentName })}
                    </span>
                  )}
                </div>
                <div className="text-sm" style={{ color: line.final ? C.textPrimary : C.textSecondary }}>
                  {line.text}
                </div>
              </div>
            ) : (
              <div key={`voice-${line.id}`} data-testid="voice-line-voice" className="shrink-0 max-w-[85%]">
                <div className="text-xs" style={{ color: C.textMuted }}>
                  {t("voiceOf", { name: agentName })}
                </div>
                <div className="text-sm" style={{ color: C.textSecondary }}>
                  {line.text}
                </div>
              </div>
            ),
          )}
        </div>
      ) : (
        !isError && (
          <div className="px-3 pt-3 text-sm" style={{ color: C.textMuted }}>
            {t("emptyHint", { name: agentName })}
          </div>
        )
      )}

      {isError ? (
        <div className="flex items-center gap-2 px-3 py-2" role="alert" data-testid="voice-error" data-error={call.error}>
          <div className="flex-1 text-sm" style={{ color: C.textPrimary }}>
            {errorText(t, call, resetTime)}
          </div>
          {isRetryable(call.error!) && (
            <button
              type="button"
              onClick={() => void call.start(call.agentId)}
              data-testid="voice-retry"
              className="shrink-0 inline-flex items-center gap-1 h-11 md:h-8 px-2 rounded-lg text-sm cursor-pointer hover:bg-[var(--color-bg-hover)]"
              style={{ color: C.textSecondary }}
            >
              <RotateCcw size={14} />
              {t("retry")}
            </button>
          )}
          <button
            type="button"
            onClick={call.dismiss}
            aria-label={t("close")}
            title={t("close")}
            data-testid="voice-dismiss"
            className="shrink-0 inline-flex items-center justify-center w-11 h-11 md:w-8 md:h-8 rounded-full cursor-pointer hover:bg-[var(--color-bg-hover)]"
            style={{ color: C.textSecondary }}
          >
            <X size={16} />
          </button>
        </div>
      ) : (
        <div className="flex items-center gap-3 px-3 py-2">
          <span
            aria-hidden
            className={`inline-block w-2 h-2 rounded-full ${
              call.status === "speaking" && !call.muted ? "motion-safe:animate-pulse" : ""
            }`}
            style={{ backgroundColor: call.status === "connecting" || call.muted ? C.textDim : C.accent }}
          />
          <span
            className="flex-1 min-w-0 text-sm"
            data-testid="voice-state"
            role={call.idleWarning ? "status" : undefined}
            style={{ color: call.idleWarning ? STATUS_TEXT.warning : C.textPrimary }}
          >
            {t(stateKey, { seconds: String(call.limits.idle_warn_s) })}
          </span>
          <CallClock call={call} />
          {call.status !== "connecting" && (
            <button
              type="button"
              onClick={call.toggleMute}
              aria-pressed={call.muted}
              aria-label={call.muted ? t("unmute") : t("mute")}
              title={call.muted ? t("unmute") : t("mute")}
              data-testid="voice-mute"
              className="shrink-0 inline-flex items-center justify-center w-11 h-11 md:w-8 md:h-8 rounded-full cursor-pointer transition-colors hover:bg-[var(--color-bg-hover)]"
              style={{
                color: call.muted ? C.textPrimary : C.textSecondary,
                backgroundColor: call.muted ? C.bgHover : undefined,
              }}
            >
              {call.muted ? <MicOff size={17} /> : <Mic size={17} />}
            </button>
          )}
          <button
            type="button"
            onClick={call.hangUp}
            data-testid="voice-hangup"
            className="shrink-0 inline-flex items-center gap-2 h-11 md:h-8 px-4 rounded-full text-sm font-medium cursor-pointer"
            style={{ backgroundColor: C.error, color: C.onStatus }}
          >
            <PhoneOff size={15} />
            {t("hangUp")}
          </button>
        </div>
      )}
    </div>
  );
}

/**
 * Wires one chat into the app-wide call (VoiceLiveCallProvider, voice PR 5):
 * the mic handed to the composer as its `voiceSlot` starts a call with THIS
 * chat's agent; while that call runs, its bar sits above the composer
 * ("docked") and listens along to this chat's stream. A call with another
 * agent shows as one compact row here instead (and no mic: one call at a
 * time, hang up first). Renders the composer unchanged (and no mic) when the
 * backend reports the feature off.
 *
 * Outside AppShell (a chat on its own, tests) it brings its own provider.
 */
export function VoiceLiveDock(props: VoiceLiveDockProps) {
  const ctx = useVoiceLiveCall();
  if (ctx) return <DockInner {...props} ctx={ctx} />;
  return (
    <VoiceLiveCallProvider env={props.env} resolveLead={false}>
      <DockWithOwnProvider {...props} />
    </VoiceLiveCallProvider>
  );
}

function DockWithOwnProvider(props: VoiceLiveDockProps) {
  const ctx = useVoiceLiveCall();
  return ctx ? <DockInner {...props} ctx={ctx} /> : <>{props.children(null)}</>;
}

interface VoiceLiveDockProps {
  agentId: string;
  agentName: string;
  children: (voiceSlot: ReactNode) => ReactNode;
  /** The voice passed a message on: echo it in the chat right away. */
  onForwarded?: (text: string, midTurn: boolean) => void;
  /** The voice stopped the agent's turn (voice PR 4): put the follow-ups
   *  taken back into the composer, like the Stop button does. */
  onStopped?: (takenBack: string[] | null) => void;
  /** The chat's live stream, for the voice to listen along (voice PR 3). */
  progress?: ProgressSnapshot;
  /** false = this chat is mounted but not visible (sessions list on the
   *  phone): it does not count as a dock, so the call floats instead. */
  onScreen?: boolean;
  /** Only used when the dock brings its own provider (tests). */
  env?: VoiceLiveEnv;
}

function DockInner({
  agentId,
  agentName,
  children,
  onForwarded,
  onStopped,
  progress,
  onScreen = true,
  env = browserVoiceLiveEnv,
  ctx,
}: VoiceLiveDockProps & { ctx: VoiceLiveCallContextValue }) {
  const { enabled, budgetReached, call, active, target, startCall, registerDock, feedProgress } = ctx;

  // Stable registration: the chat hands fresh arrows every render.
  const forwardRef = useRef(onForwarded);
  forwardRef.current = onForwarded;
  const stoppedRef = useRef(onStopped);
  stoppedRef.current = onStopped;
  useEffect(() => {
    if (!onScreen) return;
    return registerDock(agentId, {
      onForwarded: (text, midTurn) => forwardRef.current?.(text, midTurn),
      onStopped: (takenBack) => stoppedRef.current?.(takenBack),
    });
  }, [agentId, registerDock, onScreen]);

  const mine = onScreen && active && target?.agentId === agentId;
  useEffect(() => {
    if (mine) feedProgress(agentId, progress);
  }, [mine, agentId, progress, feedProgress]);

  // Read after mount: the server render has no window.
  const [secure, setSecure] = useState(true);
  useEffect(() => setSecure(env.isSecureContext()), [env]);

  if (!enabled) return <>{children(null)}</>;
  if (!onScreen) return <>{children(null)}</>;
  if (active && target && !mine) {
    return (
      <>
        <div className="mx-3 mt-2 md:mx-4">
          <VoiceCallCompact call={call} target={target} />
        </div>
        {children(null)}
      </>
    );
  }
  const slot = !mine ? (
    <VoiceMicButton
      agentName={agentName}
      secure={secure}
      budgetReached={budgetReached}
      onStart={() => startCall({ agentId, agentName })}
    />
  ) : null;
  return (
    <>
      {mine && <VoiceCallBar call={call} agentName={agentName} />}
      {children(slot)}
    </>
  );
}
