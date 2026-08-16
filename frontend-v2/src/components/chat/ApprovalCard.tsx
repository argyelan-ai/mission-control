"use client";

/**
 * ApprovalCard — renders a permission_prompt's ChatPrompt as a warning-accented
 * card: question text, one button per option, plus a quiet escape hatch to the
 * raw terminal for anyone who doesn't trust the paraphrase.
 *
 * Buttons are single-shot: a click disables the whole card immediately so a
 * double-click (or a slow network) can't fire onAnswer twice. The card
 * re-enables only when the parent hands it a fresh `prompt` object — i.e. a
 * new state event actually arrived, not just a re-render.
 */
import { useEffect, useState } from "react";
import { C } from "@/lib/colors";
import type { ChatPrompt } from "@/lib/chatTypes";

interface ApprovalCardProps {
  prompt: ChatPrompt;
  onAnswer: (key: string) => void;
  onShowTerminal: () => void;
}

export function ApprovalCard({ prompt, onAnswer, onShowTerminal }: ApprovalCardProps) {
  const [answered, setAnswered] = useState(false);

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
      className="flex flex-col gap-3 rounded-lg border px-4 py-3"
      style={{ borderColor: C.warning, backgroundColor: C.bgElevated }}
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
            style={{ borderColor: C.warning, color: C.textPrimary }}
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
        Im Terminal prüfen
      </button>
    </div>
  );
}
