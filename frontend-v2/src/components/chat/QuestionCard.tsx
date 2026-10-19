"use client";

/**
 * QuestionCard — an agent asked the operator something (Welle 3, E11).
 *
 * The same card for every agent, whatever harness it runs: `mc question`, a
 * blocking `mc ask` to the operator, or any other pending approval of this
 * agent (`useAgentQuestions`). Pinned above the composer while it waits, so
 * the phone shows it without scrolling the conversation.
 *
 * One tap where one tap is enough: each of the agent's options sends on the
 * click — the recommended one (the agent's `default`, else its first) is the
 * one primary button (K11). Free text sits behind "Other answer…"; without
 * options the text field is the card. Kinds whose approval has consequences
 * this card cannot state (installs, blockers) are shown, never hidden, and
 * link to their own card.
 *
 * Calm, not loud (E12): a waiting card wears the brightest tone (the accent
 * frame, DESIGN.md "waiting for the operator"), never ochre and never a
 * pulse. Once answered it shrinks to one quiet line that stays until the
 * next question arrives.
 */
import { useEffect, useState } from "react";
import Link from "next/link";
import { useLocale, useTranslations } from "next-intl";
import { Check, MessageCircleQuestion } from "lucide-react";

import { api } from "@/lib/api";
import { C } from "@/lib/colors";
import { notify } from "@/lib/notify";
import { timeAgo } from "@/lib/utils";
import { isQuestionNotOpenError, type OperatorQuestion } from "@/lib/chatTypes";
import { useAgentQuestions } from "@/hooks/useAgentQuestions";
import { ClampedContent } from "./ClampedContent";
import { MarkdownContent } from "./MarkdownContent";

interface QuestionCardProps {
  question: OperatorQuestion;
  /** `null` = read-only (non-admin): the question, no buttons. */
  agentId: string | null;
  /** How many more open questions wait behind this one. */
  moreCount?: number;
  /** Called after an answer was sent (with the answer) or the question
   *  turned out closed (`null`). */
  onSettled?: (answer: string | null) => void;
}

/** The option the operator most likely wants — the one primary button. */
export function primaryOption(q: OperatorQuestion): string | null {
  if (q.options.length === 0) return null;
  if (q.default && q.options.includes(q.default)) return q.default;
  return q.options[0];
}

/** Where the operator answers a kind the chat does not answer itself. */
export function elsewhereHref(q: OperatorQuestion): string {
  return q.task_id ? `/tasks?task=${encodeURIComponent(q.task_id)}` : "/inbox";
}

export function QuestionCard({ question, agentId, moreCount = 0, onSettled }: QuestionCardProps) {
  const t = useTranslations("chat.operatorQuestion");
  const locale = useLocale();
  const [sending, setSending] = useState(false);
  const [otherOpen, setOtherOpen] = useState(false);
  const [text, setText] = useState("");
  const [answered, setAnswered] = useState<string | null>(null);

  // A different question in the same slot starts fresh.
  useEffect(() => {
    setSending(false);
    setOtherOpen(false);
    setText("");
    setAnswered(null);
  }, [question.id]);

  const interactive = agentId != null && question.answerable && answered == null;
  const primary = primaryOption(question);
  const showText = interactive && (question.options.length === 0 || otherOpen);

  async function send(answer: string) {
    const value = answer.trim();
    if (!agentId || sending || !value) return;
    setSending(true);
    try {
      await api.chat.answerQuestion(agentId, question.id, value);
      setAnswered(value);
      onSettled?.(value);
    } catch (err) {
      if (isQuestionNotOpenError(err)) {
        notify.info(t("notOpen"));
        onSettled?.(null);
      } else {
        notify.error(t("failed"));
      }
    } finally {
      setSending(false);
    }
  }

  if (answered != null) {
    return <QuestionReceipt answer={answered} blocking={question.blocking} />;
  }

  const meta = [question.task_title, timeAgo(question.created_at, locale)].filter(Boolean).join(" · ");

  return (
    <div
      data-testid="question-card"
      data-status="open"
      data-source={question.source}
      role="group"
      aria-label={t("aria")}
      className="flex flex-col gap-3 rounded-lg px-3 py-3"
      style={{ border: `1px solid ${C.borderAccent}`, background: C.bgElevated }}
    >
      <div className="flex items-start gap-2">
        <span className="flex h-5 shrink-0 items-center" aria-hidden="true">
          <MessageCircleQuestion size={15} style={{ color: C.accent }} />
        </span>
        <div className="min-w-0 flex-1">
          <ClampedContent
            text={question.question}
            testId="question-text"
            className="break-words text-sm leading-snug"
            style={{ color: C.textPrimary }}
          >
            <MarkdownContent content={question.question} compact />
          </ClampedContent>
          {meta && (
            <p className="mt-1 truncate text-xs" style={{ color: C.textMuted }}>
              {meta}
            </p>
          )}
        </div>
      </div>

      {interactive && question.options.length > 0 && (
        <div className="flex flex-wrap items-center gap-2">
          {question.options.map((option) => {
            const isPrimary = option === primary;
            return (
              <button
                key={option}
                type="button"
                data-testid="question-option"
                data-primary={isPrimary || undefined}
                disabled={sending}
                onClick={() => void send(option)}
                className={`min-h-11 md:min-h-9 rounded-md px-4 text-sm transition-colors disabled:cursor-not-allowed disabled:opacity-40 ${
                  isPrimary ? "font-medium" : "hover:bg-[var(--color-bg-hover)]"
                }`}
                style={
                  isPrimary
                    ? { background: C.accent, color: C.onAccent }
                    : { color: C.textPrimary, border: `1px solid ${C.borderActive}` }
                }
              >
                {option}
              </button>
            );
          })}
          {!otherOpen && (
            <button
              type="button"
              data-testid="question-other"
              disabled={sending}
              onClick={() => setOtherOpen(true)}
              className="min-h-11 md:min-h-9 rounded-md px-3 text-sm transition-colors hover:bg-[var(--color-bg-hover)]"
              style={{ color: C.textSecondary }}
            >
              {t("other")}
            </button>
          )}
        </div>
      )}

      {showText && (
        <form
          className="flex flex-col gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            void send(text);
          }}
        >
          <textarea
            data-testid="question-text-input"
            rows={2}
            autoFocus={otherOpen}
            disabled={sending}
            value={text}
            placeholder={t("placeholder")}
            aria-label={t("placeholder")}
            onChange={(e) => setText(e.target.value)}
            className="w-full resize-y rounded-md border px-3 py-2 text-base md:text-sm outline-none"
            style={{ borderColor: C.borderActive, background: C.bgBase, color: C.textPrimary }}
          />
          <button
            type="submit"
            data-testid="question-send"
            disabled={sending || !text.trim()}
            className={`self-start min-h-11 md:min-h-9 rounded-md px-4 text-sm transition-colors disabled:cursor-not-allowed disabled:opacity-40 ${
              question.options.length === 0 ? "font-medium" : "hover:bg-[var(--color-bg-hover)]"
            }`}
            style={
              question.options.length === 0
                ? { background: C.accent, color: C.onAccent }
                : { color: C.textPrimary, border: `1px solid ${C.borderActive}` }
            }
          >
            {t("send")}
          </button>
        </form>
      )}

      {!question.answerable && (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm">
          <span style={{ color: C.textSecondary }}>{t("elsewhereHint")}</span>
          <Link
            href={elsewhereHref(question)}
            data-testid="question-elsewhere"
            className="inline-flex min-h-11 md:min-h-9 items-center underline underline-offset-4"
            style={{ color: C.textPrimary, textDecorationColor: C.borderActive }}
          >
            {t("openElsewhere")}
          </Link>
        </div>
      )}

      {question.answerable && agentId == null && (
        <p className="text-xs" style={{ color: C.textMuted }}>
          {t("waiting")}
        </p>
      )}

      {moreCount > 0 && (
        <Link
          href="/inbox"
          data-testid="question-more"
          className="self-start text-xs underline decoration-dotted underline-offset-2"
          style={{ color: C.textMuted }}
        >
          {t("more", { count: moreCount })}
        </Link>
      )}
    </div>
  );
}

/** What stays after the answer: one quiet line, until the next question. */
export function QuestionReceipt({ answer, blocking }: { answer: string; blocking: boolean }) {
  const t = useTranslations("chat.operatorQuestion");
  return (
    <div
      data-testid="question-card"
      data-status="answered"
      role="status"
      className="flex items-start gap-2 rounded-lg px-3 py-2 text-sm"
      style={{ border: `1px solid ${C.border}`, color: C.textSecondary }}
    >
      <Check size={15} className="mt-0.5 shrink-0" style={{ color: C.online }} aria-hidden="true" />
      <span className="min-w-0 break-words">
        {t("answeredWith", { answer })}
        {blocking && <span style={{ color: C.textMuted }}> · {t("resumes")}</span>}
      </span>
    </div>
  );
}

/**
 * The agent's open questions above the composer: the first one as a card
 * (blocking first, then oldest — the order the backend sends), a count of
 * the rest, and after an answer the receipt line until the next question.
 */
export function AgentQuestions({ agentId, canAnswer }: { agentId: string; canAnswer: boolean }) {
  const { questions, refresh } = useAgentQuestions(agentId);
  const [receipt, setReceipt] = useState<{ answer: string; blocking: boolean } | null>(null);

  // Another agent in the same chat pane: its receipts are not ours.
  useEffect(() => {
    setReceipt(null);
  }, [agentId]);

  const current = questions[0];
  if (!current && !receipt) return null;
  return (
    <div className="px-3 pt-2" data-region="question-card">
      {current ? (
        <QuestionCard
          question={current}
          agentId={canAnswer ? agentId : null}
          moreCount={questions.length - 1}
          onSettled={(answer) => {
            if (answer != null) setReceipt({ answer, blocking: current.blocking });
            refresh();
          }}
        />
      ) : (
        receipt && <QuestionReceipt answer={receipt.answer} blocking={receipt.blocking} />
      )}
    </div>
  );
}
