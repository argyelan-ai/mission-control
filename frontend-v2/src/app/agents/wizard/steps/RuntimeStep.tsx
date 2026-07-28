"use client";

import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { C } from "@/lib/colors";
import type { CompatMatrixHostHarness, Harness, HostHarness } from "@/lib/types";
import type { WizardAgentRuntime, WizardStepProps } from "../types";
import { initialWizardState } from "../types";
import { ModelInput, wizardLabelClass } from "../shared";
import { HarnessIcon } from "@/components/shared/HarnessIcon";

const RUNTIMES: { key: WizardAgentRuntime; label: string; hint: string }[] = [
  { key: "cli-bridge", label: "CLI Bridge (Docker)", hint: "Local container, auto-provisioned" },
  { key: "host", label: "Host (launchd)", hint: "Native binary via launchd on the Mac" },
  { key: "manual", label: "Manual", hint: "No auto-provisioning" },
];

// Host harnesses come from the backend's HostHarnessAdapter registry, shipped
// as `host_harnesses` on the compat matrix this step already fetches. The
// matrix's `harnesses`/`compatible_harnesses` stay cli-bridge-scoped (backend
// HARNESSES), so host harnesses are still filtered by wire protocol instead —
// but the protocol now travels WITH the harness.
//
// The previous hardcoded list (hermes/grok/kimi + a local protocol map) was a
// second truth: it never learned about the "claude" adapter, so a host Claude
// agent — exactly what Boss is — could not be created through the wizard at
// all, and every host harness was implicitly treated as a singleton bridge.

export function RuntimeStep({ state, update }: WizardStepProps) {
  const isHost = state.agentRuntime === "host";
  const needsHarness = state.agentRuntime === "cli-bridge" || isHost;

  const { data: matrix } = useQuery({
    queryKey: ["compat-matrix"],
    queryFn: () => api.runtimes.compatMatrix(),
    enabled: needsHarness,
  });
  const { data: runtimesData } = useQuery({
    queryKey: ["runtimes"],
    queryFn: () => api.runtimes.list(),
    enabled: needsHarness,
  });
  const { data: bridgeHealth } = useQuery({
    queryKey: ["cli-bridge-health"],
    queryFn: () => api.cliBridge.health(),
    enabled: state.agentRuntime === "cli-bridge",
    refetchInterval: 30_000,
  });
  // SINGLETON host bridges (hermes/grok/kimi) hardcode their config dir + plist
  // to one slug — provisioning a second one would clobber the first's agent.env
  // (2026-07-12 incident) and the backend 422s it. Surface that here instead of
  // letting the user build a doomed agent.
  //
  // Which harnesses that applies to is the BACKEND's answer (`singleton` on the
  // registry entry). "claude" is deliberately NOT a singleton — arbitrary claude
  // host agents are staged by host_provisioning — so the old blanket
  // "host ⇒ singleton" rule would have blocked every new host Claude agent.
  const { data: existingAgents } = useQuery({
    queryKey: ["agents", "all-for-singleton-check"],
    queryFn: () => api.agents.list(undefined, true),
    enabled: isHost,
  });
  const hostHarnesses: CompatMatrixHostHarness[] = matrix?.host_harnesses ?? [];
  const usedHostHarnesses = new Set(
    (existingAgents ?? [])
      .filter((a) => a.agent_runtime === "host" && a.harness)
      .map((a) => a.harness as string),
  );
  const isHostHarnessTaken = (h: CompatMatrixHostHarness) =>
    h.singleton && usedHostHarnesses.has(h.key);

  const matrixBySlug = new Map((matrix?.runtimes ?? []).map((r) => [r.slug, r]));
  const hostHarnessByKey = new Map(hostHarnesses.map((h) => [h.key as string, h]));

  // Whether a runtime (by matrix entry) is compatible with the chosen harness.
  // Host harnesses compare wire protocol — which now travels with the harness
  // from the registry; cli-bridge harnesses use the server-computed
  // compatible_harnesses list.
  function runtimeMatchesHarness(
    compatEntry: { protocol: string | null; compatible_harnesses: Harness[] } | undefined,
    h: Harness | HostHarness | null,
  ): boolean {
    if (!h) return true;
    if (isHost) {
      const entry = hostHarnessByKey.get(h);
      // Unknown host harness (matrix not loaded yet, or a legacy value with no
      // adapter) — nothing to compare against, so claim nothing is compatible
      // rather than silently accepting an unbindable provider.
      if (!entry) return false;
      return compatEntry?.protocol === entry.protocol;
    }
    return compatEntry?.compatible_harnesses.includes(h as Harness) ?? false;
  }

  function pickHarness(h: Harness | HostHarness) {
    // If the currently bound runtime is incompatible with the new harness,
    // clear it so the operator must re-pick a compatible provider. The model
    // string was set from that runtime's model_identifier, so it's cleared
    // too — otherwise it lingers as an orphaned value bound to nothing.
    const bound = runtimesData?.runtimes.find((r) => r.id === state.runtimeId || r.slug === state.runtimeId);
    const compatEntry = bound ? matrixBySlug.get(bound.slug ?? bound.id) : undefined;
    const stillCompatible = runtimeMatchesHarness(compatEntry, h);
    update({
      harness: h,
      runtimeId: stillCompatible ? state.runtimeId : "",
      model: stillCompatible ? state.model : initialWizardState(null).model,
    });
  }

  return (
    <div className="space-y-5">
      {/* 1. agent_runtime */}
      <div>
        <label className={wizardLabelClass}>Runtime type</label>
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
          {RUNTIMES.map((r) => {
            const active = state.agentRuntime === r.key;
            return (
              <button
                key={r.key}
                onClick={() => update({ agentRuntime: r.key })}
                className="text-left rounded-xl p-3 cursor-pointer transition-all"
                style={{
                  backgroundColor: active ? C.accentSubtle : "var(--color-bg-surface)",
                  border: `1px solid ${active ? C.borderAccent : C.borderSubtle}`,
                }}
              >
                <div
                  className="text-sm font-medium"
                  // aktiv trägt die Akzent-Fläche + der Rahmen, nicht die Textfarbe
                  style={{ color: "var(--color-text-primary)" }}
                >
                  {r.label}
                </div>
                <div className="text-[10px] text-[var(--color-text-muted)] mt-0.5">{r.hint}</div>
              </button>
            );
          })}
        </div>
      </div>

      {needsHarness && (
        <>
          {/* 2. harness — host runtime offers the host-only harnesses (ADR-064/066),
                 cli-bridge offers the server compat-matrix harnesses. */}
          <div>
            <label className={wizardLabelClass}>Harness (CLI)</label>
            <div className="flex gap-2">
              {(isHost ? hostHarnesses : matrix?.harnesses ?? []).map((h) => {
                const active = state.harness === h.key;
                const taken = isHost && isHostHarnessTaken(h as CompatMatrixHostHarness);
                return (
                  <button
                    key={h.key}
                    onClick={() => !taken && pickHarness(h.key)}
                    disabled={taken}
                    title={taken ? `Singleton – a '${h.key}' host agent already exists` : undefined}
                    className="flex-1 rounded-xl px-3 py-2.5 text-sm transition-all"
                    style={{
                      cursor: taken ? "not-allowed" : "pointer",
                      opacity: taken ? 0.4 : 1,
                      backgroundColor: active ? C.accentSubtle : "var(--color-bg-surface)",
                      border: `1px solid ${active ? C.borderAccent : C.borderSubtle}`,
                      color: "var(--color-text-primary)", // aktiv = Fläche + Rahmen
                    }}
                  >
                    <span className="inline-flex items-center justify-center gap-2">
                      <HarnessIcon harness={h.key} size={13} />
                      {h.label}
                      {taken && " ✓"}
                    </span>
                  </button>
                );
              })}
            </div>
            {isHost && state.harness === "grok" && (
              <p className="mt-1.5 text-[10px] text-[var(--color-text-muted)]">
                Grok Build talks to the xAI cloud via its own OAuth session — only
                the <code className="font-mono">grok-cloud</code> runtime is compatible.
              </p>
            )}
            {isHost && state.harness === "kimi" && (
              <p className="mt-1.5 text-[10px] text-[var(--color-text-muted)]">
                Kimi Code talks to the Moonshot cloud over its own file-based OAuth
                session — only the <code className="font-mono">kimi-cloud</code> runtime
                is compatible. After provisioning, run{" "}
                <code className="font-mono">/login</code> once in the Sessions terminal
                (device code).
              </p>
            )}
          </div>

          {/* 3. LLM runtime / provider, filtered by compat matrix */}
          <div>
            <label className={wizardLabelClass}>
              LLM Runtime / Provider {state.agentRuntime === "host" && "*"}
            </label>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 max-h-52 overflow-y-auto">
              {state.agentRuntime === "cli-bridge" && (
                <button
                  onClick={() => update({ runtimeId: "" })}
                  className="text-left rounded-lg px-3 py-2.5 text-sm cursor-pointer transition-colors"
                  style={{
                    backgroundColor: state.runtimeId === "" ? C.accentSubtle : "var(--color-bg-surface)",
                    border: `1px solid ${state.runtimeId === "" ? C.borderAccent : C.borderSubtle}`,
                    color: "var(--color-text-secondary)",
                  }}
                >
                  Fallback (docker-compose env)
                </button>
              )}
              {(runtimesData?.runtimes ?? []).map((rt) => {
                const compat = matrixBySlug.get(rt.slug ?? rt.id);
                const harnessOk = runtimeMatchesHarness(compat, state.harness);
                // Host harnesses bind single-instance runtimes on purpose (grok-cloud,
                // hermes-vLLM are single-instance host targets); the parallel-instance
                // guard runs server-side at provision. Only cli-bridge disables them here.
                const disabled = rt.enabled === false || (!isHost && !!rt.single_instance) || !harnessOk;
                const active = state.runtimeId === rt.id || state.runtimeId === rt.slug;
                const reason =
                  !harnessOk && !isHost && state.harness && compat
                    ? compat.reasons[state.harness as Harness]
                    : undefined;
                return (
                  <button
                    key={rt.id}
                    disabled={disabled}
                    title={!harnessOk ? reason : !isHost && rt.single_instance ? "single-instance runtime" : undefined}
                    onClick={() => update({ runtimeId: rt.id, model: rt.model_identifier ?? state.model })}
                    className="text-left rounded-lg px-3 py-2.5 text-sm cursor-pointer transition-colors disabled:opacity-35 disabled:cursor-not-allowed"
                    style={{
                      backgroundColor: active ? C.accentSubtle : "var(--color-bg-surface)",
                      border: `1px solid ${active ? C.borderAccent : C.borderSubtle}`,
                    }}
                  >
                    <span className="block text-[var(--color-text-primary)]">{rt.display_name}</span>
                    <span className="block text-[10px] text-[var(--color-text-muted)]">
                      {rt.runtime_type}
                      {rt.model_identifier ? ` · ${rt.model_identifier}` : ""}
                      {rt.enabled === false ? " · disabled" : ""}
                    </span>
                  </button>
                );
              })}
            </div>
          </div>

          {/* 4. model override */}
          <div>
            <label className={wizardLabelClass}>Model (optional)</label>
            <ModelInput value={state.model} onChange={(v) => update({ model: v })} />
          </div>

          {state.agentRuntime === "cli-bridge" && bridgeHealth?.reachable === false && (
            <div
              className="rounded-lg px-3 py-2 text-[11px]"
              style={{
                backgroundColor: `${C.warning}14`,
                border: `1px solid ${C.warning}33`,
                color: "var(--color-text-secondary)",
              }}
            >
              <span className="font-medium" style={{ color: C.warning }}>
                cli-bridge helper not reachable.
              </span>{" "}
              The agent will be created but stays unprovisioned until the helper is running:{" "}
              <code className="font-mono">python3 scripts/cli-bridge.py</code>
            </div>
          )}
        </>
      )}
    </div>
  );
}
