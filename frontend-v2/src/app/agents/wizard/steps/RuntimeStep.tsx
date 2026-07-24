"use client";

import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { C } from "@/lib/colors";
import type { Harness, HostHarness } from "@/lib/types";
import { HOST_HARNESS_LABELS, HOST_HARNESS_PROTOCOL } from "@/lib/types";
import type { WizardAgentRuntime, WizardStepProps } from "../types";
import { initialWizardState } from "../types";
import { ModelInput, wizardLabelClass } from "../shared";

const RUNTIMES: { key: WizardAgentRuntime; label: string; hint: string }[] = [
  { key: "cli-bridge", label: "CLI Bridge (Docker)", hint: "Lokaler Container, auto-provisioniert" },
  { key: "host", label: "Host (launchd)", hint: "Natives Binary via launchd auf dem Mac" },
  { key: "manual", label: "Manuell", hint: "Kein Auto-Provisioning" },
];

// Host-only harnesses (ADR-064/066). The compat-matrix API is cli-bridge-scoped
// (it iterates backend HARNESSES = claude/openclaude/omp), so host harnesses are
// offered from this explicit list and filtered by the runtime's wire protocol
// instead of compatible_harnesses. grok binds its own "grok" cloud runtime; a host
// grok agent MUST pick harness=grok here because derive_harness() can't infer it.
const HOST_HARNESSES: { key: HostHarness; label: string }[] = [
  { key: "hermes", label: HOST_HARNESS_LABELS.hermes },
  { key: "grok", label: HOST_HARNESS_LABELS.grok },
  { key: "kimi", label: HOST_HARNESS_LABELS.kimi },
];

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
  // hermes/grok are SINGLETON host bridges — one hermes-bridge, one grok-bridge
  // on the host, each hardcoded to its slug. Provisioning a second one would
  // clobber the first's agent.env (2026-07-12 incident), so the backend now
  // rejects it with a 422. Surface that here: disable a host harness whose
  // singleton already exists, instead of letting the user build a doomed agent.
  const { data: existingAgents } = useQuery({
    queryKey: ["agents", "all-for-singleton-check"],
    queryFn: () => api.agents.list(undefined, true),
    enabled: isHost,
  });
  const takenHostHarnesses = new Set(
    (existingAgents ?? [])
      .filter((a) => a.agent_runtime === "host" && a.harness)
      .map((a) => a.harness as string),
  );

  const matrixBySlug = new Map((matrix?.runtimes ?? []).map((r) => [r.slug, r]));

  // Whether a runtime (by matrix entry) is compatible with the chosen harness.
  // Host harnesses compare wire protocol (hermes → openai, grok → grok); cli-bridge
  // harnesses use the server-computed compatible_harnesses list.
  function runtimeMatchesHarness(
    compatEntry: { protocol: string | null; compatible_harnesses: Harness[] } | undefined,
    h: Harness | HostHarness | null,
  ): boolean {
    if (!h) return true;
    if (isHost) return compatEntry?.protocol === HOST_HARNESS_PROTOCOL[h as HostHarness];
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
        <label className={wizardLabelClass}>Runtime-Typ</label>
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
          {RUNTIMES.map((r) => {
            const active = state.agentRuntime === r.key;
            return (
              <button
                key={r.key}
                onClick={() => update({ agentRuntime: r.key })}
                className="text-left rounded-xl p-3 cursor-pointer transition-all"
                style={{
                  backgroundColor: active ? C.accentSubtle : "rgba(255,255,255,0.03)",
                  border: `1px solid ${active ? C.borderAccent : C.borderSubtle}`,
                }}
              >
                <div
                  className="text-sm font-medium"
                  style={{ color: active ? C.accent : "var(--color-text-primary)" }}
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
              {(isHost ? HOST_HARNESSES : matrix?.harnesses ?? []).map((h) => {
                const active = state.harness === h.key;
                const taken = isHost && takenHostHarnesses.has(h.key);
                return (
                  <button
                    key={h.key}
                    onClick={() => !taken && pickHarness(h.key)}
                    disabled={taken}
                    title={taken ? `Singleton – ein '${h.key}'-Host-Agent existiert bereits` : undefined}
                    className="flex-1 rounded-xl px-3 py-2.5 text-sm transition-all"
                    style={{
                      cursor: taken ? "not-allowed" : "pointer",
                      opacity: taken ? 0.4 : 1,
                      backgroundColor: active ? C.accentSubtle : "rgba(255,255,255,0.03)",
                      border: `1px solid ${active ? C.borderAccent : C.borderSubtle}`,
                      color: active ? C.accent : "var(--color-text-primary)",
                    }}
                  >
                    {h.label}
                    {taken && " ✓"}
                  </button>
                );
              })}
            </div>
            {isHost && state.harness === "grok" && (
              <p className="mt-1.5 text-[10px] text-[var(--color-text-muted)]">
                Grok Build spricht die xAI-Cloud über seine eigene OAuth-Session — nur
                die <code className="font-mono">grok-cloud</code>-Runtime ist kompatibel.
              </p>
            )}
            {isHost && state.harness === "kimi" && (
              <p className="mt-1.5 text-[10px] text-[var(--color-text-muted)]">
                Kimi Code spricht die Moonshot-Cloud über seine eigene OAuth-Datei-Session —
                nur die <code className="font-mono">kimi-cloud</code>-Runtime ist kompatibel.
                Nach dem Provisionieren einmalig <code className="font-mono">/login</code> im
                Sessions-Terminal (Device-Code).
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
                    backgroundColor: state.runtimeId === "" ? C.accentSubtle : "rgba(255,255,255,0.03)",
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
                      backgroundColor: active ? C.accentSubtle : "rgba(255,255,255,0.03)",
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
            <label className={wizardLabelClass}>Modell (optional)</label>
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
                cli-bridge Helper nicht erreichbar.
              </span>{" "}
              Agent wird erstellt, bleibt aber unprovisioniert, bis der Helper läuft:{" "}
              <code className="font-mono">python3 scripts/cli-bridge.py</code>
            </div>
          )}
        </>
      )}
    </div>
  );
}
