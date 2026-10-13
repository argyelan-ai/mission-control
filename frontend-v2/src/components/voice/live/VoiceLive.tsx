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
 */

import { type ReactNode, useEffect, useRef, useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import { Check, Mic, PhoneOff, RotateCcw, X } from "lucide-react";
import { C } from "@/lib/colors";
import { ERROR_MESSAGE_KEY, MAX_CALL_MS, isRetryable, type VoiceLiveStatus } from "./voiceLiveCore";
import { browserVoiceLiveEnv, useVoiceLive, type VoiceLiveCall, type VoiceLiveEnv } from "./useVoiceLive";
import { useVoiceLiveEnabled } from "./useVoiceLiveConfig";
import type { ProgressSnapshot } from "./progress";
import { setVoiceLiveActive } from "./voiceLiveActivity";

const STATE_KEY: Record<Exclude<VoiceLiveStatus, "idle">, string> = {
  connecting: "stateConnecting",
  listening: "stateListening",
  speaking: "stateSpeaking",
  error: "stateEnded",
};

export function VoiceMicButton({
  agentName,
  secure,
  onStart,
}: {
  agentName: string;
  secure: boolean;
  onStart: () => void;
}) {
  const t = useTranslations("voiceLive");
  const label = secure ? t("micStart", { name: agentName }) : t("micInsecure");
  return (
    <button
      type="button"
      onClick={onStart}
      aria-label={label}
      title={label}
      data-testid="voice-mic-button"
      data-secure={secure}
      className="shrink-0 inline-flex items-center justify-center w-11 h-11 md:w-8 md:h-8 rounded-full cursor-pointer transition-colors hover:bg-[var(--color-bg-hover)]"
      style={{ color: secure ? C.textSecondary : C.textDim }}
    >
      <Mic size={17} />
    </button>
  );
}

export function VoiceCallBar({ call, agentName }: { call: VoiceLiveCall; agentName: string }) {
  const t = useTranslations("voiceLive");
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
    call.passing && call.status !== "error"
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
            {t(ERROR_MESSAGE_KEY[call.error!], { minutes: String(MAX_CALL_MS / 60_000) })}
          </div>
          {isRetryable(call.error!) && (
            <button
              type="button"
              onClick={() => void call.start()}
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
            className={`inline-block w-2 h-2 rounded-full ${call.status === "speaking" ? "motion-safe:animate-pulse" : ""}`}
            style={{ backgroundColor: call.status === "connecting" ? C.textDim : C.accent }}
          />
          <span className="flex-1 text-sm" data-testid="voice-state" style={{ color: C.textPrimary }}>
            {t(stateKey)}
          </span>
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
 * Wires one chat's call: the bar above the composer plus the mic handed to
 * the composer as its `voiceSlot`. Renders the composer unchanged (and no
 * mic) when the backend reports the feature off.
 */
export function VoiceLiveDock({
  agentId,
  agentName,
  children,
  onForwarded,
  progress,
  env = browserVoiceLiveEnv,
}: {
  agentId: string;
  agentName: string;
  children: (voiceSlot: ReactNode) => ReactNode;
  /** The voice passed a message on: echo it in the chat right away. */
  onForwarded?: (text: string, midTurn: boolean) => void;
  /** The chat's live stream, for the voice to listen along (voice PR 3). */
  progress?: ProgressSnapshot;
  env?: VoiceLiveEnv;
}) {
  const locale = useLocale();
  const enabled = useVoiceLiveEnabled();
  const call = useVoiceLive(agentId, locale === "de" ? "de" : "en", env, { onForwarded, progress });
  const callActive = call.status !== "idle";
  useEffect(() => {
    setVoiceLiveActive(agentId, callActive);
    return () => setVoiceLiveActive(agentId, false);
  }, [agentId, callActive]);
  // Read after mount: the server render has no window.
  const [secure, setSecure] = useState(true);
  useEffect(() => setSecure(env.isSecureContext()), [env]);

  if (!enabled) return <>{children(null)}</>;
  const slot =
    call.status === "idle" ? (
      <VoiceMicButton agentName={agentName} secure={secure} onStart={() => void call.start()} />
    ) : null;
  return (
    <>
      <VoiceCallBar call={call} agentName={agentName} />
      {children(slot)}
    </>
  );
}
