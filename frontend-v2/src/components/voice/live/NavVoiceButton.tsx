"use client";

/**
 * The navigation mic (voice PR 5): desktop sidebar beside the search, phone
 * top bar. With the live voice on and a lead agent known it calls the LEAD
 * agent — the same live voice as in the chat. Its label carries the lead's
 * name from the data ("Voice (<name>)"); the public repo never names it.
 *
 * Fallback, unchanged from before PR 5: feature off, no lead agent, or the
 * answer still out → today's Jarvis button (`VoiceButton`, LiveKit). Jarvis
 * itself stays reachable from the menus (QuickSheet, command palette).
 *
 * While a call runs (with any agent) the mic shows it; a tap opens that
 * call's chat, where the call docks — it never starts a second call.
 */

import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { Mic } from "lucide-react";
import { C } from "@/lib/colors";
import { chatEntryHref } from "@/lib/chatHistory";
import { VoiceButton } from "@/components/voice/VoiceWidget";
import { useVoiceLiveCall } from "./VoiceLiveCall";

export function NavVoiceButton({
  size = 36,
  variant = "header",
  enforceTouchTarget = true,
  jarvisLabel,
}: {
  size?: number;
  variant?: "header" | "sidebar";
  enforceTouchTarget?: boolean;
  /** The fallback Jarvis button's label (sidebar: "Voice (Jarvis)"). */
  jarvisLabel?: string;
}) {
  const t = useTranslations("voiceLive");
  const router = useRouter();
  const ctx = useVoiceLiveCall();
  const lead = ctx?.enabled ? ctx.lead : null;

  if (!ctx || !lead) {
    return (
      <VoiceButton size={size} variant={variant} enforceTouchTarget={enforceTouchTarget} label={jarvisLabel} />
    );
  }

  const callTarget = ctx.active ? ctx.target : null;
  const label = callTarget ? t("navMicActive", { name: callTarget.agentName }) : t("navMic", { name: lead.agentName });
  const onClick = () => {
    if (callTarget) router.push(chatEntryHref("agent", callTarget.agentId));
    else ctx.startCall(lead);
  };

  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={label}
      title={label}
      data-testid="nav-voice-button"
      data-target={lead.agentId}
      data-active={callTarget !== null}
      className="flex items-center justify-center rounded-full cursor-pointer transition-colors hover:bg-[var(--color-bg-hover)]"
      style={{
        width: size,
        height: size,
        minWidth: enforceTouchTarget ? 44 : size,
        minHeight: enforceTouchTarget ? 44 : size,
        backgroundColor: callTarget ? C.accentSubtle : "transparent",
        border: callTarget
          ? `1px solid ${C.accent}`
          : variant === "sidebar"
            ? "1px solid var(--color-p2-line)"
            : "1px solid transparent",
        color: callTarget ? C.textPrimary : "var(--color-text-secondary)",
      }}
    >
      <Mic size={Math.round(size * 0.5)} />
    </button>
  );
}
