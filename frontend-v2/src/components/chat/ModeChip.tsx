"use client";

/**
 * ModeChip — Plan ⇄ Standard for a headless (ACP) agent (chat package P3).
 *
 * Shown only when the agent itself reports a plan mode
 * (`capabilities.modeOptions`, from omp's `configOptions[id=mode]`). A tap
 * switches through `POST /chat/mode`; the label follows the transcript's
 * `mode` events (omp leaves plan mode by itself once the operator approves
 * the plan — that approval arrives as a question card).
 *
 * Quiet by default (a switcher, not a state); while plan mode is on it wears
 * the selection look (accent-subtle + accent border), because then the agent
 * really behaves differently: it only reads and drafts.
 */
import { useState } from "react";
import { ListChecks } from "lucide-react";
import { useTranslations } from "next-intl";

import { api } from "@/lib/api";
import { C } from "@/lib/colors";
import { notify } from "@/lib/notify";
import { isAgentBusyError, type ChatCapabilities, type ModeEvent } from "@/lib/chatTypes";

const PLAN = "plan";

interface ModeChipProps {
  agentId: string;
  capabilities: ChatCapabilities | null;
  /** Newest live mode change from the transcript, or `null`. */
  liveMode: ModeEvent | null;
  /** The agent is mid-turn: switching then is refused, the chip waits. */
  busy?: boolean;
}

export function ModeChip({ agentId, capabilities, liveMode, busy = false }: ModeChipProps) {
  const t = useTranslations("chat.mode");
  const [pending, setPending] = useState<string | null>(null);
  /** Our own confirmed switch — it holds until the transcript brings a mode
   *  line we had not seen when we switched (no clock comparison: the phone's
   *  clock and the agent's are not the same clock). */
  const [confirmed, setConfirmed] = useState<{ mode: string; seen: string | null } | null>(null);

  const options = capabilities?.modeOptions ?? [];
  const plan = options.find((o) => o.value === PLAN);
  const other = options.find((o) => o.value !== PLAN);
  if (!plan || !other) return null;

  const live = liveMode && (!confirmed || liveMode.uuid !== confirmed.seen) ? liveMode.modeId : null;
  const current = pending ?? live ?? confirmed?.mode ?? capabilities?.mode ?? other.value;
  const isPlan = current === PLAN;
  const target = isPlan ? other.value : PLAN;
  const label = (value: string, fallback: string) => (t.has(value) ? t(value) : fallback);
  const targetLabel = label(target, target === PLAN ? plan.name : other.name);

  async function toggle() {
    if (pending || busy) return;
    setPending(target);
    try {
      await api.chat.setMode(agentId, target);
      setConfirmed({ mode: target, seen: liveMode?.uuid ?? null });
    } catch (err) {
      notify.error(isAgentBusyError(err) ? t("busy") : t("failed"));
    } finally {
      setPending(null);
    }
  }

  return (
    <button
      type="button"
      data-testid="mode-chip"
      data-mode={current}
      aria-pressed={isPlan}
      aria-label={t("switchTo", { mode: targetLabel })}
      title={busy ? t("busy") : t("planHint")}
      disabled={pending != null || busy}
      onClick={() => void toggle()}
      className="inline-flex items-center gap-1 h-9 md:h-8 px-2 rounded-full text-xs font-medium cursor-pointer transition-colors disabled:cursor-wait disabled:opacity-60"
      style={{
        border: `1px solid ${isPlan ? C.borderAccent : C.border}`,
        background: isPlan ? C.accentSubtle : "transparent",
        color: isPlan ? C.textPrimary : C.textSecondary,
      }}
    >
      <ListChecks size={14} strokeWidth={1.75} aria-hidden="true" />
      {/* Phone: icon only — the accent look already says "plan is on", and
          the composer row has no room for a word (K4). */}
      <span className="hidden sm:inline">{label(current, isPlan ? plan.name : other.name)}</span>
    </button>
  );
}
