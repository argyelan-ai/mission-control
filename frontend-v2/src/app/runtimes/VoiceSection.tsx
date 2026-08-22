"use client";

/**
 * VoiceSection — the spoken channel, kept apart from the chat runtimes (ADR-074).
 *
 * Both arms are hosted APIs, so they look like cloud rows and were listed as
 * such at first. That was wrong in a way that only shows up when you look at
 * the page: the cloud section answers "which agent runs which chat model", and
 * a realtime speech socket has no chat model, no context window and no token
 * usage to line up against the others. Sitting in that list, a voice arm reads
 * as a chat runtime nobody uses.
 *
 * So this section answers the one question that actually applies: WHICH ARM IS
 * SPEAKING right now. The bound arm is the statement, the other one is the
 * alternative — deliberately not a status dot, because like the cloud rows
 * there is no honest ready/warming state for a socket you open per call.
 */

import { useMemo } from "react";
import { useQueries } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { api } from "@/lib/api";
import { C } from "@/lib/colors";
import type { Runtime, RuntimeAgentRef } from "@/lib/types";

export function VoiceSection({
  runtimes,
  onOpen,
}: {
  runtimes: Runtime[];
  onOpen: (rt: Runtime) => void;
}) {
  const t = useTranslations("runtimes.voiceSection");

  const agentQueries = useQueries({
    queries: runtimes.map((rt) => {
      const slug = rt.slug ?? rt.id;
      return {
        queryKey: ["runtime-agents", slug],
        queryFn: () => api.runtimes.db.agents(slug),
        enabled: !!slug,
        staleTime: 15_000,
        retry: false,
      };
    }),
  });

  const rows = useMemo(
    () =>
      runtimes.map((rt, i) => ({
        runtime: rt,
        agents: (agentQueries[i]?.data?.agents ?? []) as RuntimeAgentRef[],
      })),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [runtimes, agentQueries.map((q) => q.dataUpdatedAt).join(",")]
  );

  if (runtimes.length === 0) return null;

  return (
    <section data-testid="voice-section">
      <div className="flex items-center gap-2.5 mb-1">
        <span
          className="text-[10px] font-medium uppercase shrink-0"
          style={{ color: C.textMuted, letterSpacing: "0.08em" }}
        >
          {t("title")}
        </span>
        <div className="flex-1 h-px" style={{ background: C.borderSubtle }} />
      </div>
      <p className="text-xs mb-3" style={{ color: C.textDim }}>
        {t("hint")}
      </p>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
        {rows.map(({ runtime, agents }) => {
          const bound = agents.length > 0;
          return (
            <button
              key={runtime.id}
              type="button"
              data-testid={`voice-row-${runtime.slug ?? runtime.id}`}
              onClick={() => onOpen(runtime)}
              className="flex items-center gap-2 rounded-md px-3 py-2.5 text-xs text-left cursor-pointer transition-colors hover:bg-[var(--color-bg-hover)]"
              style={{
                background: C.bgSurface,
                border: `1px solid ${bound ? C.borderAccent : C.borderSubtle}`,
                opacity: bound ? 1 : 0.65,
              }}
            >
              <span
                className="truncate"
                style={{ color: bound ? C.textPrimary : C.textMuted }}
              >
                {runtime.display_name}
              </span>
              <span className="ml-auto shrink-0" style={{ color: C.textDim }}>
                {bound
                  ? agents.map((a: RuntimeAgentRef) => a.name).join(", ")
                  : t("standby")}
              </span>
            </button>
          );
        })}
      </div>
    </section>
  );
}
