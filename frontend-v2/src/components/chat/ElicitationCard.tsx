"use client";

/**
 * ElicitationCard — a question the agent asked the operator (chat package P3).
 *
 * omp asks over ACP (`elicitation/create`, form mode) when the chat daemon
 * advertises it: a choice (`select`), yes/no (`confirm`), free text
 * (`input`), the ask tool's questions (choices with a recommended one, a
 * multi-select, an "Other…" free text), and the plan approval (two choices).
 * The card builds itself from the question's schema — no per-question code.
 *
 * One tap where one tap is enough: a single choice or yes/no question sends
 * on the click. Everything else (several questions, a multi-select, free
 * text) collects first and sends with one button.
 *
 * Its fate comes from the transcript (`elicitation_result`, same id), so
 * "answered: X" survives a reload. A click on a card the daemon no longer
 * holds (its turn ended, the chat restarted) answers 409 — the card then says
 * so instead of failing.
 *
 * Signal: accent frame only while the card waits for the operator ("needs
 * you", K8 — the brightest tone, never ochre, never a pulse: E12); once
 * settled it is a quiet row.
 */
import { useMemo, useState } from "react";
import { MessageCircleQuestion } from "lucide-react";
import { useTranslations } from "next-intl";

import { api } from "@/lib/api";
import { C } from "@/lib/colors";
import { notify } from "@/lib/notify";
import {
  isElicitationNotPendingError,
  type ElicitationChoice,
  type ElicitationEvent,
  type ElicitationProperty,
  type ElicitationResultEvent,
} from "@/lib/chatTypes";
import { ClampedContent } from "./ClampedContent";
import { MarkdownContent } from "./MarkdownContent";
import { TIMELINE_CARD_GUTTER } from "./timelineCard";

const OTHER_SUFFIX = "__other";

interface ElicitationCardProps {
  ev: ElicitationEvent;
  result: ElicitationResultEvent | null;
  /** `null` = read-only view (a head's transcript): outcome only, no buttons. */
  agentId: string | null;
}

type Answers = Record<string, string | boolean | string[]>;

interface Field {
  key: string;
  prop: ElicitationProperty;
  choices: ElicitationChoice[];
  /** The ask tool's free-text twin (`<key>__other`), if offered. */
  otherKey: string | null;
}

function choicesOf(prop: ElicitationProperty): ElicitationChoice[] {
  if (prop.type === "array") return prop.items?.anyOf ?? [];
  if (prop.oneOf?.length) return prop.oneOf;
  return (prop.enum ?? []).map((value) => ({ const: value, title: value }));
}

export function fieldsOf(ev: ElicitationEvent): Field[] {
  const props = ev.schema.properties;
  return Object.keys(props)
    .filter((key) => !key.endsWith(OTHER_SUFFIX))
    .map((key) => ({
      key,
      prop: props[key],
      choices: choicesOf(props[key]),
      otherKey: `${key}${OTHER_SUFFIX}` in props ? `${key}${OTHER_SUFFIX}` : null,
    }));
}

/** One tap answers it: exactly one question, and it is a choice or yes/no. */
function isOneTap(fields: Field[]): boolean {
  if (fields.length !== 1) return false;
  const { prop, choices } = fields[0];
  return prop.type === "boolean" || (prop.type === "string" && choices.length > 0);
}

/** What the operator answered, in words — for the settled card. */
export function summarize(
  content: Record<string, unknown> | null | undefined,
  yes: string,
  no: string,
): string {
  if (!content) return "";
  return Object.values(content)
    .map((v) => {
      if (typeof v === "boolean") return v ? yes : no;
      if (Array.isArray(v)) return v.filter((x) => typeof x === "string").join(", ");
      return typeof v === "string" || typeof v === "number" ? String(v).trim() : "";
    })
    .filter(Boolean)
    .join(" · ");
}

function hasValue(v: unknown): boolean {
  if (typeof v === "boolean") return true;
  if (Array.isArray(v)) return v.length > 0;
  return typeof v === "string" && v.trim().length > 0;
}

export function ElicitationCard({ ev, result, agentId }: ElicitationCardProps) {
  const t = useTranslations("chat.question");
  const fields = useMemo(() => fieldsOf(ev), [ev]);
  const oneTap = isOneTap(fields);
  const [answers, setAnswers] = useState<Answers>({});
  const [otherOpen, setOtherOpen] = useState<Record<string, boolean>>({});
  const [sending, setSending] = useState(false);
  /** Set locally when the daemon said "no longer open" before the transcript did. */
  const [gone, setGone] = useState(false);
  /** What this tab just sent — shown until the transcript line arrives. */
  const [sent, setSent] = useState<{ status: string; content?: Answers } | null>(null);

  const status = result?.status ?? (gone ? "expired" : sent?.status ?? null);
  const open = status == null;
  const interactive = open && agentId != null;

  async function submit(action: "accept" | "decline" | "cancel", content?: Answers) {
    if (!agentId || sending) return;
    setSending(true);
    try {
      const res = await api.chat.answerElicitation(agentId, ev.elicitationId, action, content);
      setSent({ status: res.status, content });
    } catch (err) {
      if (isElicitationNotPendingError(err)) {
        setGone(true);
        notify.info(t("notOpen"));
      } else {
        notify.error(t("failed"));
      }
    } finally {
      setSending(false);
    }
  }

  function pick(field: Field, value: string | boolean) {
    if (oneTap) {
      void submit("accept", { [field.key]: value });
      return;
    }
    setAnswers((prev) => {
      if (field.prop.type === "array") {
        const current = Array.isArray(prev[field.key]) ? (prev[field.key] as string[]) : [];
        const next = current.includes(value as string)
          ? current.filter((v) => v !== value)
          : [...current, value as string];
        return { ...prev, [field.key]: next };
      }
      return { ...prev, [field.key]: value };
    });
  }

  const required = ev.schema.required ?? [];
  const canSend =
    !sending &&
    required.every((key) => hasValue(answers[key])) &&
    Object.values(answers).some(hasValue);

  function sendForm() {
    const content: Answers = {};
    for (const [key, value] of Object.entries(answers)) {
      if (hasValue(value)) content[key] = typeof value === "string" ? value.trim() : value;
    }
    void submit("accept", content);
  }

  const settledText = (() => {
    if (status === "answered") {
      const said = summarize(result?.content ?? sent?.content, t("yes"), t("no"));
      return said ? t("answeredWith", { answer: said }) : t("answered");
    }
    if (status === "declined") return t("declined");
    if (status === "cancelled") return ev.planApproval ? t("cancelledStopped") : t("cancelled");
    if (status === "expired") return t("expired");
    return agentId == null ? t("waiting") : null;
  })();

  return (
    <div className={TIMELINE_CARD_GUTTER}>
      <div
        data-testid="elicitation-card"
        data-status={status ?? "open"}
        role="group"
        aria-label={t("aria")}
        className="flex flex-col gap-3 rounded-lg px-3 py-3"
        style={{
          border: `1px solid ${interactive ? C.borderAccent : C.border}`,
          background: interactive ? C.bgElevated : "transparent",
        }}
      >
        <div className="flex items-start gap-2">
          {/* One text line high, so the icon sits on the first line. */}
          <span className="flex h-5 shrink-0 items-center" aria-hidden="true">
            <MessageCircleQuestion size={15} style={{ color: interactive ? C.accent : C.textMuted }} />
          </span>
          {/* One column for text AND its "Show more": ClampedContent renders
              the toggle as a sibling, which in this row became a second
              column squeezing the text (live, 390 px). Plans arrive as
              markdown (# title, lists, code) — rendered, not raw. */}
          <div className="min-w-0 flex-1">
            <ClampedContent
              text={ev.message}
              testId="elicitation-message"
              className="break-words text-sm leading-snug"
              style={{ color: open ? C.textPrimary : C.textSecondary }}
            >
              <MarkdownContent content={ev.message} compact />
            </ClampedContent>
          </div>
        </div>

        {interactive &&
          fields.map((field) => (
            <FieldInput
              key={field.key}
              field={field}
              showTitle={fields.length > 1 || (field.prop.title ?? "") !== ev.message}
              value={answers[field.key]}
              otherValue={field.otherKey ? (answers[field.otherKey] as string | undefined) : undefined}
              otherOpen={!!otherOpen[field.key]}
              disabled={sending}
              oneTap={oneTap}
              onPick={(v) => pick(field, v)}
              onText={(v) => setAnswers((prev) => ({ ...prev, [field.key]: v }))}
              onOpenOther={() => setOtherOpen((prev) => ({ ...prev, [field.key]: true }))}
              onOther={(v) => field.otherKey && setAnswers((prev) => ({ ...prev, [field.otherKey!]: v }))}
            />
          ))}

        {interactive && (
          <div className="flex flex-wrap items-center gap-2">
            {(!oneTap || Object.values(otherOpen).some(Boolean)) && (
              <button
                type="button"
                data-testid="elicitation-send"
                disabled={!canSend}
                onClick={sendForm}
                className="min-h-11 md:min-h-9 rounded-md px-4 text-sm font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-40"
                style={{ background: C.accent, color: C.onAccent }}
              >
                {t("send")}
              </button>
            )}
            <button
              type="button"
              data-testid="elicitation-skip"
              data-stops={ev.planApproval || undefined}
              disabled={sending}
              onClick={() => void submit("cancel")}
              className="min-h-11 md:min-h-9 rounded-md px-3 text-sm transition-colors hover:bg-[var(--color-bg-hover)] disabled:opacity-40"
              style={{ color: C.textSecondary }}
            >
              {/* Plan approval: omp reads a plain cancel as "keep planning"
                  and asks again at once — the daemon then also stops the
                  turn, and the button says so (live 13.10.2026). */}
              {ev.planApproval ? t("cancelStops") : t("skip")}
            </button>
          </div>
        )}

        {settledText && (
          <p data-testid="elicitation-outcome" className="text-xs" style={{ color: C.textMuted }}>
            {settledText}
          </p>
        )}
      </div>
    </div>
  );
}

interface FieldInputProps {
  field: Field;
  showTitle: boolean;
  value: Answers[string] | undefined;
  otherValue: string | undefined;
  otherOpen: boolean;
  disabled: boolean;
  oneTap: boolean;
  onPick: (value: string | boolean) => void;
  onText: (value: string) => void;
  onOpenOther: () => void;
  onOther: (value: string) => void;
}

function FieldInput({
  field, showTitle, value, otherValue, otherOpen, disabled, oneTap,
  onPick, onText, onOpenOther, onOther,
}: FieldInputProps) {
  const t = useTranslations("chat.question");
  const { prop, choices } = field;
  const title = prop.title?.trim();
  const recommended = typeof prop.default === "string" ? prop.default : null;

  const choiceButton = (key: string, label: string, val: string | boolean, hint?: string) => {
    const selected = Array.isArray(value) ? value.includes(val as string) : value === val;
    const isRecommended = recommended != null && val === recommended;
    return (
      <button
        key={key}
        type="button"
        disabled={disabled}
        aria-pressed={oneTap ? undefined : selected}
        data-testid="elicitation-choice"
        data-recommended={isRecommended || undefined}
        title={hint}
        onClick={() => onPick(val)}
        className="min-h-11 md:min-h-9 rounded-md border px-3 text-left text-sm transition-colors disabled:cursor-not-allowed disabled:opacity-40"
        style={{
          borderColor: selected || isRecommended ? C.borderAccent : C.borderActive,
          background: selected ? C.accentSubtle : "transparent",
          color: C.textPrimary,
        }}
      >
        {label}
        {isRecommended && (
          <span className="ml-2 text-xs" style={{ color: C.textMuted }}>
            {t("recommended")}
          </span>
        )}
      </button>
    );
  };

  return (
    <div className="flex flex-col gap-2" data-testid={`elicitation-field-${field.key}`}>
      {showTitle && title && (
        <div className="text-sm" style={{ color: C.textSecondary }}>
          {title}
          {prop.description && (
            <span className="ml-2 text-xs" style={{ color: C.textMuted }}>
              {prop.description}
            </span>
          )}
        </div>
      )}

      {prop.type === "boolean" ? (
        <div className="flex flex-wrap gap-2">
          {choiceButton("yes", t("yes"), true)}
          {choiceButton("no", t("no"), false)}
        </div>
      ) : choices.length > 0 ? (
        <div className="flex flex-wrap gap-2">
          {choices.map((c) => choiceButton(c.const, c.title, c.const, c.description))}
          {field.otherKey && !otherOpen && (
            <button
              type="button"
              disabled={disabled}
              data-testid="elicitation-other"
              onClick={onOpenOther}
              className="min-h-11 md:min-h-9 rounded-md px-3 text-sm transition-colors hover:bg-[var(--color-bg-hover)]"
              style={{ color: C.textSecondary }}
            >
              {t("other")}
            </button>
          )}
        </div>
      ) : (
        <textarea
          data-testid="elicitation-text"
          disabled={disabled}
          rows={2}
          value={typeof value === "string" ? value : ""}
          placeholder={prop.description ?? t("placeholder")}
          aria-label={title ?? t("placeholder")}
          onChange={(e) => onText(e.target.value)}
          className="w-full resize-y rounded-md border px-3 py-2 text-sm outline-none"
          style={{ borderColor: C.borderActive, background: C.bgBase, color: C.textPrimary }}
        />
      )}

      {field.otherKey && otherOpen && (
        <textarea
          data-testid="elicitation-other-text"
          disabled={disabled}
          rows={2}
          autoFocus
          value={otherValue ?? ""}
          placeholder={t("otherPlaceholder")}
          aria-label={t("other")}
          onChange={(e) => onOther(e.target.value)}
          className="w-full resize-y rounded-md border px-3 py-2 text-sm outline-none"
          style={{ borderColor: C.borderActive, background: C.bgBase, color: C.textPrimary }}
        />
      )}
    </div>
  );
}
