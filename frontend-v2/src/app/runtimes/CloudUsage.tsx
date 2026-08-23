"use client";

/**
 * CloudUsage — "who's using what" for hosted APIs (mockup M1, m1-slot-buehne.html,
 * CLOUD section).
 *
 * Hosted APIs (Anthropic/Claude, Grok, Kimi, ...) have no honest lifecycle
 * state to show — there is no "ready/warming/starting" for a call you make
 * over the internet. So unlike SlotStage, this section renders NO status
 * dots at all: just who is bound to which model. Runtimes with zero bound
 * agents are noise (nobody is using them right now) and are hidden behind a
 * trailing dashed row until the operator asks to see them.
 */

import { useMemo, useState } from "react";
import { useQueries } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { ChevronDown } from "lucide-react";
import Link from "next/link";
import { api } from "@/lib/api";
import { C, STATUS_TEXT } from "@/lib/colors";
import type { Runtime, RuntimeAgentRef } from "@/lib/types";

// ── Agent-bound row ────────────────────────────────────────────────────────

function UsageRow({
  runtime,
  agents,
  onOpen,
  pendingLabel,
  pendingTitle,
}: {
  runtime: Runtime;
  agents: RuntimeAgentRef[];
  onOpen: (rt: Runtime) => void;
  pendingLabel: string;
  pendingTitle: string;
}) {
  const shown = agents.slice(0, 3);
  const overflow = agents.length - shown.length;
  const anyPending = agents.some((a) => a.pending_runtime_sync);

  return (
    <div
      role="button"
      tabIndex={0}
      data-testid={`cloud-usage-row-${runtime.slug ?? runtime.id}`}
      onClick={() => onOpen(runtime)}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onOpen(runtime);
        }
      }}
      className="flex items-center justify-between gap-3 px-3.5 py-2.5 rounded-lg cursor-pointer transition-colors hover:bg-[var(--color-bg-hover)]"
      style={{ background: C.bgSurface, border: `1px solid ${C.borderSubtle}` }}
    >
      <span className="flex items-center gap-1.5 flex-wrap min-w-0">
        {shown.map((a) => (
          <Link
            key={a.id}
            href={`/agents/${a.id}`}
            onClick={(e) => e.stopPropagation()}
            className="font-mono text-[11px] leading-none rounded-sm px-2 py-1 hover:bg-[var(--color-bg-hover)] transition-colors"
            style={{ background: C.accentSubtle, border: `1px solid ${C.borderAccent}`, color: C.textSecondary }}
          >
            {a.name}
          </Link>
        ))}
        {overflow > 0 && (
          <span
            className="font-mono text-[11px] leading-none rounded-sm px-2 py-1"
            style={{ background: C.accentSubtle, border: `1px solid ${C.borderAccent}`, color: C.textSecondary }}
          >
            +{overflow}
          </span>
        )}
        {anyPending && (
          <span
            className="text-[11px] leading-none rounded-sm px-2 py-1 shrink-0"
            style={{ color: STATUS_TEXT.warning, border: `1px solid ${C.warning}66` }}
            title={pendingTitle}
          >
            {pendingLabel}
          </span>
        )}
      </span>
      <span className="font-mono text-xs shrink-0" style={{ color: C.textSecondary }}>
        {runtime.model_identifier ?? runtime.display_name}
      </span>
    </div>
  );
}

// ── Zero-agent row (only rendered once expanded) ──────────────────────────

function UnboundRow({ runtime, onOpen, bindCta }: { runtime: Runtime; onOpen: (rt: Runtime) => void; bindCta: string }) {
  return (
    <button
      type="button"
      data-testid={`cloud-usage-row-${runtime.slug ?? runtime.id}`}
      onClick={() => onOpen(runtime)}
      className="flex items-center justify-between gap-3 px-3.5 py-2.5 rounded-lg text-left cursor-pointer transition-colors hover:bg-[var(--color-bg-hover)]"
      style={{ background: C.bgSurface, border: `1px solid ${C.borderSubtle}`, opacity: 0.6 }}
    >
      <span className="text-xs truncate" style={{ color: C.textMuted }}>
        {runtime.display_name}
      </span>
      <span className="text-xs shrink-0" style={{ color: C.textDim }}>
        {bindCta}
      </span>
    </button>
  );
}

// ── Root ───────────────────────────────────────────────────────────────────

export function CloudUsage({
  runtimes,
  onOpen,
}: {
  runtimes: Runtime[];
  onOpen: (rt: Runtime) => void;
}) {
  const t = useTranslations("runtimes.cloudUsage");
  const tr = useTranslations("runtimes");
  const [expanded, setExpanded] = useState(false);

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

  // Every row starts pending with `agents: []` — indistinguishable from a
  // genuinely unbound runtime until its own query settles. Without tracking
  // that separately, a bound runtime flickers through the "no agent" collapse
  // row (with a briefly-inflated count) before its chips appear. `settled`
  // keeps unsettled rows out of both buckets until they're actually known.
  const rows = useMemo(
    () =>
      runtimes.map((rt, i) => ({
        runtime: rt,
        agents: agentQueries[i]?.data?.agents ?? [],
        settled: agentQueries[i]?.isPending !== true,
      })),
    // agentQueries is a fresh array every render (per useQueries semantics) —
    // its .data/.isPending identities are what actually matter for
    // memoization, but keying off the array itself is correct and cheap here
    // (small lists).
    [runtimes, agentQueries]
  );

  if (runtimes.length === 0) return null;

  const anyPending = agentQueries.some((q) => q.isPending);
  const bound = rows.filter((r) => r.settled && r.agents.length > 0);
  const unbound = rows.filter((r) => r.settled && r.agents.length === 0);

  return (
    <section className="mt-8">
      <div className="flex items-center gap-2.5 mb-3">
        <span
          className="text-[10px] font-medium uppercase shrink-0"
          style={{ color: C.textMuted, letterSpacing: "0.08em" }}
        >
          {t("sectionTitle")}
        </span>
        <div className="flex-1 h-px" style={{ background: C.borderSubtle }} />
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
        {bound.map(({ runtime, agents }) => (
          <UsageRow
            key={runtime.id}
            runtime={runtime}
            agents={agents}
            onOpen={onOpen}
            pendingLabel={tr("pendingSync")}
            pendingTitle={tr("pendingSyncTitle")}
          />
        ))}

        {!anyPending && unbound.length > 0 && (
          <>
            <button
              type="button"
              data-testid="cloud-usage-collapse-toggle"
              onClick={() => setExpanded((v) => !v)}
              aria-expanded={expanded}
              className="flex items-center justify-between gap-2 px-3.5 py-2.5 rounded-lg cursor-pointer transition-colors hover:bg-[var(--color-bg-hover)]"
              style={{ borderStyle: "dashed", borderWidth: "1px", borderColor: C.borderActive, color: C.textDim }}
            >
              <span className="text-xs">{t("collapseRow", { n: unbound.length })}</span>
              <ChevronDown
                size={13}
                aria-hidden
                style={{ transform: expanded ? "rotate(180deg)" : "none", transition: "transform 0.15s" }}
              />
            </button>

            {expanded &&
              unbound.map(({ runtime }) => (
                <UnboundRow key={runtime.id} runtime={runtime} onOpen={onOpen} bindCta={t("bindCta")} />
              ))}
          </>
        )}
      </div>
    </section>
  );
}
