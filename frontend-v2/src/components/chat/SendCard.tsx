"use client";

/**
 * SendCard — an agent sent a message to another session (ADR-100 §4, 2d).
 *
 * The call is `mc send <to> <text>` through a shell tool or the MCP tool
 * `mc_send`; as a tool row it read "$ mc send claude@desk:runtimes …" inside
 * a collapsed group, which hid the one thing the operator wants to see: the
 * agent just talked to someone else. The card names the receiver, the text
 * and the delivery, and says "info only" — a message never grants a right
 * (§6). It is not a button: the conversation itself lives in the inbox tab
 * "Between agents".
 *
 * A refused send is a failed step, not a failed turn: a warning dot and the
 * reason, no red (DESIGN K20).
 */
import { useTranslations } from "next-intl";
import { Send } from "lucide-react";
import { C, STATUS_TEXT, stepFailedStyle } from "@/lib/colors";
import type { ToolEvent } from "@/lib/chatTypes";
import { DELIVERY_KEYS, sendOutcome, type SendCall } from "@/lib/comms";
import { Pill } from "@/components/shared/Pill";

export function SendCard({ ev, call }: { ev: ToolEvent; call: SendCall }) {
  const t = useTranslations("comms");
  const outcome = sendOutcome(ev);
  const failed = stepFailedStyle();
  const lead =
    outcome.kind === "failed" ? t("notSentTo") : outcome.kind === "pending" ? t("sendingTo") : t("sentTo");
  const delivery =
    outcome.kind === "sent" && outcome.delivery && DELIVERY_KEYS[outcome.delivery]
      ? t(DELIVERY_KEYS[outcome.delivery])
      : null;

  return (
    <div className="w-full px-4 py-1">
      <div
        data-testid="send-card"
        data-outcome={outcome.kind}
        className="rounded-lg px-4 py-3 flex flex-col gap-2"
        style={{ background: C.bgSurface, border: `1px solid ${C.border}` }}
      >
        <div className="flex items-center gap-2 min-w-0">
          {outcome.kind === "failed" ? (
            <span
              className="inline-block w-1.5 h-1.5 rounded-full shrink-0"
              style={{ background: failed.dot }}
              aria-hidden
            />
          ) : (
            <Send size={13} className="shrink-0" style={{ color: C.textMuted }} aria-hidden />
          )}
          <span className="text-xs shrink-0" style={{ color: C.textMuted }}>
            {lead}
          </span>
          <span className="flex-1 min-w-0 truncate font-mono text-xs" style={{ color: C.textPrimary }}>
            {call.to}
          </span>
          {outcome.kind !== "failed" && (
            <Pill color={C.textMuted} casing="sentence" className="shrink-0">
              {t("infoOnly")}
            </Pill>
          )}
        </div>

        {call.body != null ? (
          <p className="m-0 text-sm line-clamp-3 whitespace-pre-wrap break-words" dir="auto" style={{ color: C.textSecondary }}>
            {call.body}
          </p>
        ) : (
          <p className="m-0 text-xs" style={{ color: C.textMuted }}>
            {t("fromStdin")}
          </p>
        )}

        {outcome.kind === "failed" && outcome.reason && (
          <p className="m-0 text-xs break-words" style={{ color: C.textMuted }}>
            {outcome.reason}
          </p>
        )}
        {(delivery || (outcome.kind === "sent" && outcome.paused)) && (
          <p className="m-0 text-xs" style={{ color: C.textMuted }}>
            {delivery}
            {delivery && outcome.kind === "sent" && outcome.paused && " · "}
            {outcome.kind === "sent" && outcome.paused && (
              <span style={{ color: STATUS_TEXT.warning }}>{t("conversationPaused")}</span>
            )}
          </p>
        )}
      </div>
    </div>
  );
}
