"use client";

import { useQuery } from "@tanstack/react-query";
import { Sparkles, LayoutTemplate, Copy } from "lucide-react";
import { api } from "@/lib/api";
import { C } from "@/lib/colors";
import type { AgentTemplate, Agent, Harness } from "@/lib/types";
import type { StartMode, WizardStepProps } from "../types";
import { initialWizardState } from "../types";

const MODES: { key: StartMode; label: string; icon: typeof Sparkles; hint: string }[] = [
  { key: "custom", label: "Individuell", icon: Sparkles, hint: "Von Grund auf konfigurieren" },
  { key: "template", label: "Vorlage", icon: LayoutTemplate, hint: "Aus einem Rollen-Template" },
  { key: "duplicate", label: "Duplizieren", icon: Copy, hint: "Bestehenden Agent kopieren" },
];

// Prefillable fields only — board/step are shell-owned and must survive a mode switch.
const RESET_PREFILL = initialWizardState(null);

export function StartStep({ state, update }: WizardStepProps) {
  const { data: templates } = useQuery({
    queryKey: ["agent-templates"],
    queryFn: () => api.agentTemplates.list(),
    enabled: state.startMode === "template",
  });
  const { data: agents } = useQuery({
    queryKey: ["agents", "all-for-duplicate"],
    queryFn: () => api.agents.list(undefined, true),
    enabled: state.startMode === "duplicate",
  });

  function pickTemplate(t: AgentTemplate) {
    update({
      startMode: "template",
      templateId: t.id,
      name: t.name,
      emoji: t.emoji ?? "",
      role: t.role ?? "",
      soulMd: t.soul_md,
      scopes: t.scopes,
      model: t.default_model ?? "",
      skillFilter: t.skill_filter,
      cliPlugins: t.cli_plugins,
    });
  }

  async function pickAgent(a: Agent) {
    // Load the full source config so the duplicate is faithful.
    const src = await api.agents.get(a.id);
    update({
      startMode: "duplicate",
      sourceAgentId: src.id,
      name: `${src.name} Kopie`,
      emoji: src.emoji ?? "",
      role: src.role ?? "",
      soulMd: src.soul_md,
      scopes: src.scopes,
      model: src.model ?? "",
      skillFilter: src.skill_filter,
      cliPlugins: src.cli_plugins,
      agentRuntime:
        src.agent_runtime === "host" || src.agent_runtime === "manual"
          ? src.agent_runtime
          : "cli-bridge",
      harness: (src.harness as Harness | null) ?? null,
      runtimeId: src.runtime_id ?? "",
      isBoardLead: false,
    });
  }

  return (
    <div className="space-y-5">
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
        {MODES.map((m) => {
          const active = state.startMode === m.key;
          const Icon = m.icon;
          return (
            <button
              key={m.key}
              onClick={() =>
                update({
                  startMode: m.key,
                  templateId: null,
                  sourceAgentId: null,
                  name: RESET_PREFILL.name,
                  emoji: RESET_PREFILL.emoji,
                  role: RESET_PREFILL.role,
                  soulMd: RESET_PREFILL.soulMd,
                  agentRuntime: RESET_PREFILL.agentRuntime,
                  harness: RESET_PREFILL.harness,
                  runtimeId: RESET_PREFILL.runtimeId,
                  model: RESET_PREFILL.model,
                  scopes: RESET_PREFILL.scopes,
                  skillFilter: RESET_PREFILL.skillFilter,
                  cliPlugins: RESET_PREFILL.cliPlugins,
                })
              }
              className="text-left rounded-xl p-4 cursor-pointer transition-all"
              style={{
                backgroundColor: active ? C.accentSubtle : "rgba(255,255,255,0.03)",
                border: `1px solid ${active ? C.borderAccent : C.borderSubtle}`,
              }}
            >
              <Icon size={18} style={{ color: active ? C.accent : "var(--color-text-muted)" }} />
              <div
                className="text-sm font-medium mt-2"
                style={{ color: active ? C.accent : "var(--color-text-primary)" }}
              >
                {m.label}
              </div>
              <div className="text-[11px] text-[var(--color-text-muted)] mt-0.5">{m.hint}</div>
            </button>
          );
        })}
      </div>

      {state.startMode === "template" && (
        <div className="space-y-2">
          <div className="text-[11px] uppercase tracking-wider text-[var(--color-text-muted)]">
            Template wählen
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 max-h-64 overflow-y-auto">
            {(templates ?? []).map((t) => {
              const active = state.templateId === t.id;
              return (
                <button
                  key={t.id}
                  onClick={() => pickTemplate(t)}
                  className="flex items-center gap-2 text-left rounded-lg px-3 py-2.5 cursor-pointer transition-colors"
                  style={{
                    backgroundColor: active ? C.accentSubtle : "rgba(255,255,255,0.03)",
                    border: `1px solid ${active ? C.borderAccent : C.borderSubtle}`,
                  }}
                >
                  <span className="text-lg">{t.emoji}</span>
                  <span className="flex-1">
                    <span className="block text-sm text-[var(--color-text-primary)]">{t.name}</span>
                    <span className="block text-[10px] text-[var(--color-text-muted)]">
                      {t.role ?? "—"}
                    </span>
                  </span>
                </button>
              );
            })}
          </div>
        </div>
      )}

      {state.startMode === "duplicate" && (
        <div className="space-y-2">
          <div className="text-[11px] uppercase tracking-wider text-[var(--color-text-muted)]">
            Agent zum Duplizieren wählen
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 max-h-64 overflow-y-auto">
            {(agents ?? []).map((a) => {
              const active = state.sourceAgentId === a.id;
              return (
                <button
                  key={a.id}
                  onClick={() => pickAgent(a)}
                  className="flex items-center gap-2 text-left rounded-lg px-3 py-2.5 cursor-pointer transition-colors"
                  style={{
                    backgroundColor: active ? C.accentSubtle : "rgba(255,255,255,0.03)",
                    border: `1px solid ${active ? C.borderAccent : C.borderSubtle}`,
                  }}
                >
                  <span className="text-lg">{a.emoji ?? "🤖"}</span>
                  <span className="flex-1">
                    <span className="block text-sm text-[var(--color-text-primary)]">{a.name}</span>
                    <span className="block text-[10px] text-[var(--color-text-muted)]">
                      {a.role ?? "—"} · {a.agent_runtime}
                    </span>
                  </span>
                </button>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
