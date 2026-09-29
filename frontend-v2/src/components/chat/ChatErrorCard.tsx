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
 */
import { AlertTriangle } from "lucide-react";
import { useTranslations } from "next-intl";

import { C, STATUS_TEXT } from "@/lib/colors";
import type { MessageEvent } from "@/lib/chatTypes";
import { ClampedContent } from "./ClampedContent";

interface ChatErrorCardProps {
  ev: MessageEvent;
}

export function ChatErrorCard({ ev }: ChatErrorCardProps) {
  const t = useTranslations("chat");
  const code = ev.error?.code ?? "generic";
  // Ein Code, fuer den wir keinen Text haben, darf nicht als roher Punkt-Pfad
  // im Chat landen — der Sammelbegriff ist die ehrlichere Aussage.
  const labelKey = `error.${code}`;
  const label = t.has(labelKey) ? t(labelKey) : t("error.generic");
  const detail = ev.error?.detail?.trim() || ev.text;

  return (
    <div className="w-full px-4 md:px-5 py-1">
      <div
        data-testid="chat-error-card"
        data-code={code}
        className="rounded-lg px-2.5 py-2"
        style={{ border: `1px solid ${C.error}`, background: C.bgElevated }}
      >
        <div className="flex items-center gap-2">
          <AlertTriangle size={14} style={{ color: STATUS_TEXT.error }} aria-hidden="true" />
          <span
            data-testid="chat-error-code"
            data-code={code}
            className="rounded px-1.5 py-0.5 text-[11px] font-medium"
            style={{ color: STATUS_TEXT.error, border: `1px solid ${C.error}` }}
          >
            {label}
          </span>
        </div>
        {detail && (
          <ClampedContent
            text={detail}
            testId="chat-error-detail"
            className="mt-1.5 break-words whitespace-pre-wrap text-[13px] leading-[1.5]"
            style={{ color: C.textSecondary }}
          >
            {detail}
          </ClampedContent>
        )}
      </div>
    </div>
  );
}
