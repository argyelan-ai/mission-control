"use client";

import { useContext } from "react";
import { QueryClientContext, useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { api } from "@/lib/api";
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

/** Pure render — tested without queries. Nothing known → nothing shown (K3). */
export function SpeedLineView({ box, model, tps, live = false }: SpeedLineFacts) {
  const t = useTranslations("cockpit.speed");
  if (!box && !model) return null;
  const parts = [box, model].filter(Boolean).join(" · ");
  return (
    <div
      data-testid="speed-line"
      aria-label={t("label")}
      className="flex items-center gap-2 px-4 pb-2 md:pb-3 text-xs min-w-0"
      style={{ color: C.textMuted }}
    >
      <span
        aria-hidden
        className="w-1.5 h-1.5 rounded-full shrink-0"
        style={{ background: live ? C.info : C.textDim }}
      />
      <span className="truncate min-w-0">{parts}</span>
      {/* A local box always shows a speed: live while streaming, else the
          last measured one, else "– tok/s". A cloud model has no box and
          no measured speed — nothing is shown then (K3). */}
      {box && (
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
    </div>
  );
}

/**
 * The speed line under the composer (variant C, operator decision
 * 2026-11-18): dot · box · model · live tok/s. Model from the transcript
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

function SpeedLineLoaded({ runtimeId, model, streaming }: SpeedLineProps) {
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
    <SpeedLineView
      box={boxLabel(runtime)}
      model={model ?? runtime?.model_identifier ?? null}
      tps={live ?? lastTps(pulse)}
      live={live != null}
    />
  );
}
