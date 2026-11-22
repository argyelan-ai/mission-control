"use client";

import { useContext, useState } from "react";
import { QueryClientContext, useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { ChevronsUpDown } from "lucide-react";
import { api, type RecordChat } from "@/lib/api";
import { canPickPair, useChatSwitch } from "@/hooks/useChatSwitch";
import { useIsAdmin } from "@/hooks/useIsAdmin";
import { TaskStepsAgentContext } from "@/hooks/useTaskStepDetail";
import { PairPicker } from "./PairPicker";
import { C } from "@/lib/colors";
import { useRuntimeRows } from "@/lib/runtimeRows";
import type { HostPulse, Runtime } from "@/lib/types";

export type SpeedLineFacts = {
  /** Box (or boxes) the chat's model runs on, e.g. "Box A+B"; null = cloud / unknown. */
  box: string | null;
  model: string | null;
  /** Tokens per second of that box; null = never measured. */
  tps: number | null;
  /** true = measured right now while this chat streams; false = the last
   *  value the box measured (the operator wants a number at rest too). */
  live?: boolean;
  /** A turn runs in this chat. Only then does a box without any measured
   *  speed say "– tok/s"; at rest an empty placeholder is noise (K3). */
  streaming?: boolean;
  /** Set = the line is the door to the pair picker (chat record on). */
  onPick?: () => void;
};

/** The box part of the line: the runtime's head box plus its duo members. */
export function boxLabel(runtime: Pick<Runtime, "host" | "member_hosts"> | null | undefined): string | null {
  if (!runtime?.host) return null;
  const names = [runtime.host.display_name, ...(runtime.member_hosts ?? []).map((m) => m.display_name)]
    .filter((n): n is string => !!n);
  return [...new Set(names)].join(" + ") || null;
}

/**
 * Live tok/s only while this chat streams and only when the box measures it
 * (GET /hosts/{id}/pulse, `now_tps` — the same number the runtimes stage
 * shows). It is the speed of the whole box: other chats on the same box
 * count in, which the tooltip says.
 */
export function liveTps(pulse: HostPulse | undefined, streaming: boolean): number | null {
  if (!streaming || !pulse?.available || pulse.now_tps == null) return null;
  const tps = Math.round(pulse.now_tps);
  return tps > 0 ? tps : null;
}

/**
 * The last speed the box measured, for a chat at rest: the newest pulse point
 * above zero (the ring the runtimes stage draws). null = nothing measured.
 */
export function lastTps(pulse: HostPulse | undefined): number | null {
  if (!pulse?.available) return null;
  for (let i = pulse.points.length - 1; i >= 0; i--) {
    const tps = Math.round(pulse.points[i].tps);
    if (tps > 0) return tps;
  }
  return null;
}

/**
 * A model id as a person reads it: no provider path ("mc-openai/…") and no
 * "custom:" prefix — nothing else is cut. The family name and a numeric tail
 * ("-1017", "-2411", "-0528") are what tell two models apart, and the
 * composer chip is the only place the model is named (critic 21.11.2026: a
 * rule that dropped them turned "Qwen-V3" into "V3" and "Mistral-Large-2411"
 * into "Mistral-Large"). A display name from the model catalogue wins when
 * the caller has one. Empty is null, so a caller's `?? id` still catches it.
 */
export function shortModelName(id: string | null | undefined, displayName?: string | null): string | null {
  if (displayName && displayName.trim()) return displayName.trim();
  if (!id || !id.trim()) return null;
  let name = id.trim().replace(/^custom:/i, "");
  // "provider/model" has no spaces; "GPT-5.1 / codex" is a label, not a path.
  if (!/\s/.test(name)) name = name.slice(name.lastIndexOf("/") + 1);
  return name.trim() || null;
}

/** Pure render — tested without queries. Nothing known → nothing shown (K3). */
export function SpeedLineView({ box, model, tps, live = false, streaming = false, onPick }: SpeedLineFacts) {
  const t = useTranslations("cockpit.speed");
  const ts = useTranslations("cockpit.switch");
  // The line names the whole model again (operator 22.11.2026): on a
  // 390 px phone the composer's chip gives way to Send/Stop and cuts the
  // name ("DeepSeek-V…"). The chip stays; this line is where the full name
  // can be read — without the provider path, never cut (it wraps instead).
  // A cloud model without a box and without the picker door gets no line:
  // its chip is not cut, and "fable" under "Fable" only repeats it.
  if (!box && !onPick) return null;
  const parts = [box, shortModelName(model)].filter(Boolean).join(" · ");
  if (!parts) return null;
  const showTps = !!box && (tps != null || streaming);
  const body = (
    <>
      <span
        aria-hidden
        className="w-1.5 h-1.5 rounded-full shrink-0"
        style={{ background: live ? C.info : C.textDim }}
      />
      <span data-testid="speed-line-label" className="min-w-0 [overflow-wrap:anywhere]">{parts}</span>
      {/* A local box shows a speed: live while streaming, else the last
          measured one; "– tok/s" only while a turn runs and nothing was
          ever measured. A cloud model has no box and no measured speed —
          nothing is shown then (K3). */}
      {showTps && (
        <span
          data-testid="speed-line-tps"
          data-live={live || undefined}
          title={live ? t("boxTps") : tps != null ? t("lastTps") : t("noTps")}
          className="font-mono tabular-nums shrink-0"
          style={{ color: live ? C.textSecondary : C.textMuted }}
        >
          {tps != null ? t("tps", { tps }) : t("tpsNone")}
        </span>
      )}
      {onPick && <ChevronsUpDown size={12} aria-hidden="true" className="shrink-0" style={{ color: C.textMuted }} />}
    </>
  );
  const className = "flex items-center gap-2 px-4 pb-2 md:pb-3 text-xs min-w-0";
  if (onPick) {
    // The same line, tappable: the hit area grows to 44 px above and below
    // (before:) without moving the composer (K11).
    return (
      <button
        type="button"
        data-testid="speed-line"
        aria-label={ts("open")}
        aria-haspopup="dialog"
        onClick={onPick}
        className={`${className} relative w-full text-left cursor-pointer before:absolute before:inset-x-0 before:-inset-y-2 before:content-['']`}
        style={{ color: C.textMuted }}
      >
        {body}
      </button>
    );
  }
  return (
    <div data-testid="speed-line" aria-label={t("label")} className={className} style={{ color: C.textMuted }}>
      {body}
    </div>
  );
}

/** The pair the line names. The record chat row is the truth (chat_out
 *  carries its runtimeId/model; the picker refreshes it after a switch),
 *  never the switch rows: an agent runtime switch writes no switch row.
 *  The main chat follows the agent, so it names the agent binding. */
export function pairForLine(
  chat: Pick<RecordChat, "agentDefault" | "runtimeId" | "model"> | null,
  fallback: { runtimeId: string | null; model: string | null },
): { runtimeId: string | null; model: string | null } {
  if (!chat || chat.agentDefault) return fallback;
  return { runtimeId: chat.runtimeId ?? null, model: chat.model ?? null };
}

/**
 * The speed line under the composer (variant C, operator decision
 * 2026-11-18): dot · box · model · live tok/s; the model by its whole
 * name, since the chip cuts it on a phone (22.11.2026). Model from the transcript
 * (usage) first, the agent's runtime row second; the box from that row's
 * host binding (ADR-048); the tok/s from the box's pulse.
 */
type SpeedLineProps = { runtimeId: string | null; model: string | null; streaming: boolean };

export function SpeedLine(props: SpeedLineProps) {
  // Outside a query client (a chat rendered on its own, e.g. in a unit test)
  // there is nothing to look up — the line steps aside instead of throwing.
  if (!useContext(QueryClientContext)) return null;
  return <SpeedLineLoaded {...props} />;
}

function SpeedLineLoaded({ runtimeId: boundRuntime, model: liveModel, streaming }: SpeedLineProps) {
  // The chat switch (ADR-105) — only while the chat record is on; off, the
  // line is exactly the one before.
  const agentId = useContext(TaskStepsAgentContext);
  const sw = useChatSwitch(agentId);
  const isAdmin = useIsAdmin();
  const [picking, setPicking] = useState(false);
  const door = canPickPair(sw.chat, isAdmin);
  const { runtimeId, model } = pairForLine(sw.chat, { runtimeId: boundRuntime, model: liveModel });
  const { data: rows } = useRuntimeRows(!!runtimeId);
  const runtime = runtimeId ? rows?.runtimes.find((r) => r.id === runtimeId) ?? null : null;
  const hostId = runtime?.host?.id ?? null;
  const { data: pulse } = useQuery({
    queryKey: ["hosts", hostId, "pulse"],
    queryFn: () => api.hosts.pulse(hostId!),
    enabled: !!hostId,
    // Fast while this chat streams, slow at rest (only the last value is shown).
    refetchInterval: streaming ? 5_000 : 60_000,
  });
  const live = liveTps(pulse, streaming);
  return (
    <>
      <SpeedLineView
        box={boxLabel(runtime)}
        model={model ?? runtime?.model_identifier ?? null}
        tps={live ?? lastTps(pulse)}
        live={live != null}
        streaming={streaming}
        onPick={door ? () => setPicking(true) : undefined}
      />
      {door && sw.chat && (
        <PairPicker
          open={picking}
          onClose={() => setPicking(false)}
          chat={sw.chat}
          chats={sw.allChats}
          agentId={agentId}
        />
      )}
    </>
  );
}
