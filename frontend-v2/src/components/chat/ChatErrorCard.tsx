"use client";

/**
 * ChatErrorCard — eine Stoerung des Chat-Daemons als sichtbares Ereignis.
 *
 * Bei einem ACP-Agenten ist der Chat die einzige Oberflaeche (Spec
 * docs/specs/chat-over-acp.md): es gibt kein Terminal, in dem man den Grund
 * nachsehen koennte. Ein abgebrochener Zug muss darum IM Verlauf stehen und
 * dort anders aussehen als alles andere — sonst liest er sich als eine
 * weitere ruhige Zeile „von aussen".
 *
 * Signal-Doktrin: Rot ist hier Zustand, nicht Schmuck. Es traegt den Rahmen
 * und den Code-Chip; der Wortlaut der Quelle bleibt im ruhigen Fliesstext,
 * damit der Text lesbar und nicht laut ist.
 *
 * S1-Rest (bauplan §9): Fuer die Codes, deren `detail` eine Maschinen-Zeile
 * ist („turn 4", omps ECONNREFUSED-Zeile), steht ein uebersetzter Satz als
 * Haupttext da und das rohe `detail` nur klein darunter — sonst saehe der
 * Operator nach einem Neustart eine rote Karte mit „turn 4" bzw. englischen
 * Text unter einem deutschen Etikett. Traegt die Karte den eigenen Text des
 * Operators (`retryText`), bietet sie EINEN Tipp „Nochmal senden" an — nur
 * wenn ChatView sie dafuer freigibt (`chatResend.tsx`), nie von selbst — und
 * zeigt genau diesen Text als Zitat darueber: nach einem Modellwechsel landet
 * die Karte in einem NEUEN Gespraech, in dem die Blase des Operators fehlt.
 *
 * Ausnahme `busy` (Usertest Runde 1, 22.10.2026): „der Agent antwortete noch"
 * ist laut DESIGN.md ein Info-Zustand, kein Fehler (K6). Die Karte steht
 * darum ruhig da — neutraler Rahmen und Chip, das Info-Blau nur am Symbol —
 * mit einem Satz in der Sprache der Oberflaeche statt omps englischer Zeile
 * „a turn is already running" (K10). Alte Karten bleiben im Verlauf stehen,
 * deshalb sitzt das hier und nicht nur beim Erzeuger.
 */
import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { AlertTriangle, Check, ChevronRight, Info, RotateCcw } from "lucide-react";
import { useTranslations } from "next-intl";

import { C, STATUS_TEXT } from "@/lib/colors";
import type { MessageEvent } from "@/lib/chatTypes";
import { ClampedContent } from "./ClampedContent";
import { retryTextOf, useChatResend } from "./chatResend";
import { TIMELINE_CARD_GUTTER } from "./timelineCard";

interface ChatErrorCardProps {
  ev: MessageEvent;
}

/** `detail` values that name a CASE of a code (S1-R4 writes them for
 *  `session_reset`) — they choose the sentence and are not printed again. */
const DETAIL_CASE = /^[a-z][a-z_]*$/;

/** A detail that only counts turns ("turn 4", "child exited during turn 4")
 *  tells the operator nothing the sentence does not (K3), and it is English
 *  jargon under a sentence in the UI language (K10). */
const TURN_COUNTER = /^(?:child exited during )?turn \d+$/;

/** omp's own refusal line on a `busy` card: the sentence says it in the UI
 *  language, the English original adds nothing (K3/K10). */
const BUSY_LINE = /^a turn is already running\.?$/i;

/** Codes that report a state, not a failure: drawn neutral (K6). */
const INFO_CODES = new Set(["busy"]);

/** `process_exit` after a failed respawn of the agent process — its own
 *  case: the chat is NOT reconnecting. The exception after it is the cause. */
const RESTART_FAILED = /^restart failed:\s*/;

/** Which sentence a card gets and what of the raw detail stays as small
 *  print: the case of a code first (`session_reset` + `model_missing`), then
 *  the code itself. Codes without a sentence keep the detail as their text. */
function cardText(
  code: string,
  rawDetail: string,
  has: (key: string) => boolean,
): { bodyKey: string | null; small: string } {
  let caseName: string | null = null;
  let rest = rawDetail;
  if (DETAIL_CASE.test(rawDetail)) {
    caseName = rawDetail;
    rest = "";
  } else if (code === "process_exit" && RESTART_FAILED.test(rawDetail)) {
    caseName = "restart_failed";
    rest = rawDetail.replace(RESTART_FAILED, "");
  }
  const caseKey = caseName ? `error.${code}_${caseName}Body` : null;
  const bodyKey =
    caseKey && has(caseKey)
      ? caseKey
      : code === "model_unreachable"
        ? "error.modelUnreachableHint"
        : has(`error.${code}Body`)
          ? `error.${code}Body`
          : null;
  if (!bodyKey) return { bodyKey: null, small: "" };
  // A case word that chose no sentence of its own is still information.
  const small = bodyKey === caseKey ? rest : rawDetail;
  return { bodyKey, small: TURN_COUNTER.test(small) || BUSY_LINE.test(small) ? "" : small };
}

/** ready → sent (tapped, waiting for ChatView to see the echo) → seen (the
 *  echo or the operator's line is there). */
type ResendPhase = "ready" | "sent" | "seen";

export function ChatErrorCard({ ev }: ChatErrorCardProps) {
  const t = useTranslations("chat");
  const { resend, canResend } = useChatResend();
  const code = ev.error?.code ?? "generic";
  // Ein Code, fuer den wir keinen Text haben, darf nicht als roher Punkt-Pfad
  // im Chat landen — der Sammelbegriff ist die ehrlichere Aussage.
  const labelKey = `error.${code}`;
  const label = t.has(labelKey) ? t(labelKey) : t("error.generic");
  const detail = ev.error?.detail?.trim() || ev.text;
  const rawDetail = ev.error?.detail?.trim() ?? "";

  // The sentence in the operator's language; the raw detail only as small
  // print, and only where it carries a cause (K3: never twice, no counters).
  const { bodyKey, small: smallDetail } = cardText(code, rawDetail, (key) => t.has(key));
  const body = bodyKey ? t(bodyKey) : null;

  const retryText = retryTextOf(ev);
  const offered = canResend(ev.uuid);
  const [phase, setPhase] = useState<ResendPhase>("ready");
  // A second tap in the same frame must not send twice — the disabled
  // attribute arrives only with the next render.
  const lockRef = useRef(false);
  useEffect(() => {
    if (phase === "sent" && !offered) setPhase("seen");
    // Offered again after the echo was seen: it went away without an
    // operator line (the request failed, or the message was taken back).
    if (phase === "seen" && offered) {
      lockRef.current = false;
      setPhase("ready");
    }
  }, [phase, offered]);
  const showResend = retryText !== null && (offered || phase !== "ready");
  const sent = phase !== "ready";

  function handleResend() {
    if (retryText === null || lockRef.current) return;
    lockRef.current = true;
    setPhase("sent");
    resend(retryText);
  }

  const showLink = code === "model_unreachable";
  // Info (busy): neutral frame and chip, the one status colour at the icon.
  const info = INFO_CODES.has(code);
  const frame = info ? C.border : C.error;
  const Icon = info ? Info : AlertTriangle;

  return (
    <div className={TIMELINE_CARD_GUTTER}>
      <div
        data-testid="chat-error-card"
        data-code={code}
        data-tone={info ? "info" : "error"}
        className="rounded-lg px-2.5 py-2"
        style={{ border: `1px solid ${frame}`, background: C.bgElevated }}
      >
        <div className="flex items-center gap-2">
          <Icon size={14} style={{ color: info ? STATUS_TEXT.info : STATUS_TEXT.error }} aria-hidden="true" />
          <span
            data-testid="chat-error-code"
            data-code={code}
            className="rounded px-1.5 py-0.5 text-[11px] font-medium"
            style={{ color: info ? C.textSecondary : STATUS_TEXT.error, border: `1px solid ${frame}` }}
          >
            {label}
          </span>
        </div>
        {body ? (
          <>
            <p
              data-testid="chat-error-body"
              className="mt-2 break-words text-sm leading-normal"
              style={{ color: C.textSecondary }}
            >
              {body}
            </p>
            {smallDetail && (
              // An exception can run to 4000 characters (session_reset,
              // process_exit) — clamped like before this card had a sentence,
              // with its line breaks kept.
              <ClampedContent
                text={smallDetail}
                testId="chat-error-detail"
                className="mt-1 break-words whitespace-pre-wrap font-mono text-xs leading-normal"
                style={{ color: C.textMuted }}
              >
                {smallDetail}
              </ClampedContent>
            )}
          </>
        ) : (
          detail && (
            <ClampedContent
              text={detail}
              testId="chat-error-detail"
              className="mt-1.5 break-words whitespace-pre-wrap text-[13px] leading-[1.5]"
              style={{ color: C.textSecondary }}
            >
              {detail}
            </ClampedContent>
          )
        )}
        {showResend && retryText !== null && (
          // What the button sends, word for word. After a model change the
          // restart opens a NEW conversation, so the operator's own bubble is
          // not on this page (plan review F1) — the tap must not be blind.
          // Quotation marks at the card's own edge, not an indented bar: an
          // indent would be a third left edge in the card (K9).
          <ClampedContent
            text={retryText}
            testId="chat-error-retry-text"
            className="mt-2 break-words whitespace-pre-wrap text-sm leading-normal"
            style={{ color: C.textPrimary }}
          >
            <span className="sr-only">{t("error.resendQuoteLabel")}: </span>
            {t("error.resendQuote", { text: retryText })}
          </ClampedContent>
        )}
        {showLink && (
          // Part of the text, at its left edge — never in the button's row.
          // 44 px at every width like the button below: one finger-target
          // rule in the card (K11), a tablet is a finger, too.
          <Link
            href="/runtimes"
            data-testid="chat-error-runtimes-link"
            className="inline-flex min-h-11 items-center gap-1 text-sm font-medium"
            style={{ color: C.accent }}
          >
            {t("error.runtimesLink")}
            <ChevronRight size={14} aria-hidden="true" />
          </Link>
        )}
        {showResend && (
          // Layout B (operator choice 20.10.): the card's last row — a
          // hairline across the content, then the button over the full width,
          // label centred. Nothing wraps next to it at 320 px or large text.
          // 44 px at every width (K11); the line in the calm border tone, the
          // card's red frame already carries the signal (K6). The hover
          // surface gets the controls' radius (DESIGN.md Buttons, rounded-md)
          // at its two free corners only: its top edge IS the hairline and
          // stays straight, the bottom echoes the card's own corners.
          <button
            type="button"
            data-testid="chat-error-resend"
            onClick={handleResend}
            disabled={sent}
            className="mt-2 flex w-full min-h-11 items-center justify-center gap-2 rounded-b-md text-sm font-medium transition-colors hover:bg-[var(--color-bg-hover)] disabled:cursor-default disabled:hover:bg-transparent"
            style={{ color: sent ? C.textMuted : C.accent, borderTop: `1px solid ${C.border}` }}
          >
            {sent ? <Check size={14} aria-hidden="true" /> : <RotateCcw size={14} aria-hidden="true" />}
            {sent ? t("error.resent") : t("error.resend")}
          </button>
        )}
      </div>
    </div>
  );
}
