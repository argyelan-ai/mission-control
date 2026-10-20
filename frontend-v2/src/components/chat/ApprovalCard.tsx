"use client";

/**
 * ApprovalCard — renders a permission_prompt's ChatPrompt as an accent-framed
 * card (waiting for the operator = the brightest tone, never ochre — E12): question text, one button per option, plus a quiet escape hatch to the
 * raw terminal for anyone who doesn't trust the paraphrase.
 *
 * Buttons are single-shot: a click disables the whole card immediately so a
 * double-click (or a slow network) can't fire onAnswer twice. The card
 * re-enables only when the parent hands it a fresh `prompt` object — i.e. a
 * new state event actually arrived, not just a re-render.
 *
 * `attention` (voice PR 4): during a live voice call the voice says "bitte im
 * Chat antippen" — the card then scrolls into view and pulses once, so the
 * operator finds what to tap. The voice itself never answers the card.
 */
import { useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { C } from "@/lib/colors";
import type { ChatPrompt } from "@/lib/chatTypes";

interface ApprovalCardProps {
  prompt: ChatPrompt;
  onAnswer: (key: string) => void;
  onShowTerminal: () => void;
  /** A voice call is open in this chat: draw the eye to the card. */
  attention?: boolean;
}

/** How long the one-time pulse runs. */
const ATTENTION_PULSE_MS = 2_000;

export function ApprovalCard({ prompt, onAnswer, onShowTerminal, attention = false }: ApprovalCardProps) {
  const t = useTranslations("chat.approval");
  const [answered, setAnswered] = useState(false);
  const [pulsing, setPulsing] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  // Once per prompt (a new state event), not on every re-render.
  useEffect(() => {
    if (!attention) return;
    const reduce =
      typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    ref.current?.scrollIntoView?.({ block: "nearest", behavior: reduce ? "auto" : "smooth" });
    setPulsing(true);
    const id = setTimeout(() => setPulsing(false), ATTENTION_PULSE_MS);
    return () => clearTimeout(id);
  }, [prompt, attention]);

  // New prompt object === new state event from the tailer → re-arm.
  useEffect(() => {
    setAnswered(false);
  }, [prompt]);

  function handleAnswer(key: string) {
    setAnswered(true);
    onAnswer(key);
  }

  return (
    <div
      ref={ref}
      data-attention={attention}
      className={`flex flex-col gap-3 rounded-lg border px-4 py-3 ${pulsing ? "motion-safe:animate-pulse" : ""}`}
      style={{ borderColor: C.borderAccent, backgroundColor: C.bgElevated }}
    >
      <p className="text-sm leading-snug" style={{ color: C.textPrimary }}>
        {prompt.question}
      </p>
      <div className="flex flex-wrap gap-2">
        {prompt.options.map((option) => (
          <button
            key={option.key}
            type="button"
            disabled={answered}
            onClick={() => handleAnswer(option.key)}
            className="rounded-md border px-3 py-1.5 text-sm transition-colors disabled:cursor-not-allowed disabled:opacity-40"
            style={{ borderColor: C.borderActive, color: C.textPrimary }}
          >
            {option.label}
          </button>
        ))}
      </div>
      <button
        type="button"
        onClick={onShowTerminal}
        className="self-start text-xs underline decoration-dotted underline-offset-2"
        style={{ color: C.textMuted }}
      >
        {t("checkTerminal")}
      </button>
    </div>
  );
}
