"use client";

import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { C } from "@/lib/colors";
import type { Harness } from "@/lib/types";
import type { WizardAgentRuntime, WizardStepProps } from "../types";
import { initialWizardState } from "../types";
import { ModelInput, wizardLabelClass } from "../shared";

const RUNTIMES: { key: WizardAgentRuntime; label: string; hint: string }[] = [
  { key: "cli-bridge", label: "CLI Bridge (Docker)", hint: "Lokaler Container, auto-provisioniert" },
  { key: "host", label: "Host (launchd)", hint: "Natives Binary via launchd auf dem Mac" },
  { key: "manual", label: "Manuell", hint: "Kein Auto-Provisioning" },
];

export function RuntimeStep({ state, update }: WizardStepProps) {
  const needsHarness = state.agentRuntime === "cli-bridge" || state.agentRuntime === "host";

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

  const matrixBySlug = new Map((matrix?.runtimes ?? []).map((r) => [r.slug, r]));

  function pickHarness(h: Harness) {
    // If the currently bound runtime is incompatible with the new harness,
    // clear it so the operator must re-pick a compatible provider. The model
    // string was set from that runtime's model_identifier, so it's cleared
    // too — otherwise it lingers as an orphaned value bound to nothing.
    const bound = runtimesData?.runtimes.find((r) => r.id === state.runtimeId || r.slug === state.runtimeId);
    const compatEntry = bound ? matrixBySlug.get(bound.slug ?? bound.id) : undefined;
    const stillCompatible = compatEntry ? compatEntry.compatible_harnesses.includes(h) : true;
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
          {/* 2. harness */}
          <div>
            <label className={wizardLabelClass}>Harness (CLI)</label>
            <div className="flex gap-2">
              {(matrix?.harnesses ?? []).map((h) => {
                const active = state.harness === h.key;
                return (
                  <button
                    key={h.key}
                    onClick={() => pickHarness(h.key)}
                    className="flex-1 rounded-xl px-3 py-2.5 text-sm cursor-pointer transition-all"
                    style={{
                      backgroundColor: active ? C.accentSubtle : "rgba(255,255,255,0.03)",
                      border: `1px solid ${active ? C.borderAccent : C.borderSubtle}`,
                      color: active ? C.accent : "var(--color-text-primary)",
                    }}
                  >
                    {h.label}
                  </button>
                );
              })}
            </div>
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
                const harnessOk = state.harness ? compat?.compatible_harnesses.includes(state.harness) ?? false : true;
                const disabled = rt.enabled === false || !!rt.single_instance || !harnessOk;
                const active = state.runtimeId === rt.id || state.runtimeId === rt.slug;
                const reason = state.harness && compat ? compat.reasons[state.harness] : undefined;
                return (
                  <button
                    key={rt.id}
                    disabled={disabled}
                    title={!harnessOk ? reason : rt.single_instance ? "single-instance runtime" : undefined}
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
