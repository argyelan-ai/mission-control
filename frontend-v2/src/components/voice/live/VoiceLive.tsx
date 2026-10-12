"use client";

/**
 * Live voice in the chat — the visible part (voice PR 1).
 *
 * - `VoiceMicButton`: secondary icon button in the composer, left of send
 *   (DESIGN K11: send stays the one primary action). Greyed on plain http,
 *   but still tappable so the operator learns *why* (a disabled button on a
 *   phone explains nothing).
 * - `VoiceCallBar`: sits above the composer while a call runs or failed:
 *   live captions ("by voice" bubbles for the operator, a quiet line for the
 *   voice), one state word, hang-up. The composer stays usable below — in
 *   PR 1 the voice cannot pass anything on, so typing is the way to the agent.
 */

import { type ReactNode, useEffect, useRef, useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import { Mic, PhoneOff, RotateCcw, X } from "lucide-react";
import { C } from "@/lib/colors";
import { ERROR_MESSAGE_KEY, MAX_CALL_MS, isRetryable, type VoiceLiveStatus } from "./voiceLiveCore";
import { browserVoiceLiveEnv, useVoiceLive, type VoiceLiveCall, type VoiceLiveEnv } from "./useVoiceLive";
import { useVoiceLiveEnabled } from "./useVoiceLiveConfig";

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

  useEffect(() => {
    const el = listRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [lineCount, lastText]);

  if (call.status === "idle") return null;
  const isError = call.status === "error" && call.error !== null;
  const hasYou = call.transcript.some((l) => l.who === "you");

  return (
    <div
      data-region="voice-call"
      data-testid="voice-call-bar"
      data-status={call.status}
      className="mx-3 mt-2 md:mx-4 rounded-xl"
      style={{ backgroundColor: C.bgSurface, border: `1px solid ${C.border}` }}
    >
      {lineCount > 0 ? (
        <div
          ref={listRef}
          data-testid="voice-transcript"
          aria-live="polite"
          className="max-h-48 overflow-y-auto px-3 pt-3 flex flex-col gap-2"
        >
          {call.transcript.map((line) =>
            line.who === "you" ? (
              <div
                key={`you-${line.id}`}
                data-testid="voice-line-you"
                className="self-end max-w-[85%] rounded-xl px-3 py-2"
                style={{ backgroundColor: C.accentSubtle }}
              >
                <div className="text-xs" style={{ color: C.textMuted }}>
                  {t("byVoice")}
                </div>
                <div className="text-sm" style={{ color: line.final ? C.textPrimary : C.textSecondary }}>
                  {line.text}
                </div>
              </div>
            ) : (
              <div key={`voice-${line.id}`} data-testid="voice-line-voice" className="max-w-[85%]">
                <div className="text-xs" style={{ color: C.textMuted }}>
                  {t("voiceOf", { name: agentName })}
                </div>
                <div className="text-sm" style={{ color: C.textSecondary }}>
                  {line.text}
                </div>
              </div>
            ),
          )}
          {hasYou && (
            <div data-testid="voice-not-sent" className="text-xs pb-1" style={{ color: C.textMuted }}>
              {t("notSentYet", { name: agentName })}
            </div>
          )}
        </div>
      ) : (
        !isError && (
          <div className="px-3 pt-3 text-sm" style={{ color: C.textMuted }}>
            {t("emptyHint")}
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
            {t(STATE_KEY[call.status as Exclude<VoiceLiveStatus, "idle">])}
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
  env = browserVoiceLiveEnv,
}: {
  agentId: string;
  agentName: string;
  children: (voiceSlot: ReactNode) => ReactNode;
  env?: VoiceLiveEnv;
}) {
  const locale = useLocale();
  const enabled = useVoiceLiveEnabled();
  const call = useVoiceLive(agentId, locale === "de" ? "de" : "en", env);
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
