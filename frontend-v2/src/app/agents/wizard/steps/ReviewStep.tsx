"use client";

import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2, CheckCircle2, XCircle } from "lucide-react";
import { api } from "@/lib/api";
import { notify } from "@/lib/notify";
import { C } from "@/lib/colors";
import type { WizardStepProps } from "../types";
import { TokenDisplay, wizardBtnPrimaryStyle, wizardLabelClass } from "../shared";

export function ReviewStep({
  state,
  update,
  onCreated,
}: WizardStepProps & { onCreated: () => void }) {
  const qc = useQueryClient();
  const [creating, setCreating] = useState(false);
  // Mirror the create result locally — `update` lifts it into wizard state
  // for the shell (back/close), but this component must not depend on that
  // round-trip to flip into the post-create view on the same render pass.
  const [localResult, setLocalResult] = useState<{ id: string; token: string | null } | null>(null);

  const isCliBridge = state.agentRuntime === "cli-bridge";
  const createdAgentId = localResult?.id ?? state.createdAgentId;
  const createdToken = localResult?.token ?? state.createdToken;

  async function handleCreate() {
    if (creating || createdAgentId) return;
    setCreating(true);
    try {
      const created = await api.agents.create({
        name: state.name.trim(),
        emoji: state.emoji.trim() || undefined,
        role: state.role.trim() || undefined,
        model: state.model.trim() || undefined,
        board_id: state.boardId || undefined,
        is_board_lead: state.isBoardLead,
        agent_runtime: state.agentRuntime,
        runtime_id: (isCliBridge || state.agentRuntime === "host") && state.runtimeId ? state.runtimeId : undefined,
        harness: state.harness ?? undefined,
        scopes: state.scopes,
        soul_md: state.soulMd ?? undefined,
        skill_filter: state.skillFilter,
        cli_plugins: state.cliPlugins,
      });
      const createdWithToken = created as typeof created & { token?: string };
      const token = createdWithToken.token ?? null;
      setLocalResult({ id: created.id, token });
      update({ createdAgentId: created.id, createdToken: token });
      notify.success(`Agent "${state.name}" erstellt`);

      // Host agents don't auto-provision on create — stage their files now.
      if (state.agentRuntime === "host") {
        try {
          await api.agents.provision(created.id);
        } catch {
          notify.error("Host-Dateien konnten nicht gerendert werden — später via Provision-Button.");
        }
      }
      await qc.refetchQueries({ queryKey: ["agents"] });
    } catch (e: unknown) {
      notify.error(`Erstellen fehlgeschlagen: ${e instanceof Error ? e.message : "Fehler"}`);
    } finally {
      setCreating(false);
    }
  }

  // Readiness poll — only after creation. ready = provisioned + live.
  const { data: readiness } = useQuery({
    queryKey: ["agent-readiness", createdAgentId],
    queryFn: () => api.agents.healthCheck(createdAgentId as string),
    enabled: !!createdAgentId,
    refetchInterval: (q) => (q.state.data?.ready ? false : 4000),
  });

  const rows: { label: string; value: string }[] = [
    { label: "Name", value: `${state.emoji || "🤖"} ${state.name}` },
    { label: "Rolle", value: state.role || "—" },
    { label: "Runtime", value: state.agentRuntime },
    { label: "Harness", value: state.harness ?? "(abgeleitet)" },
    { label: "LLM Runtime", value: state.runtimeId || "Fallback" },
    { label: "Modell", value: state.model || "(Runtime-Default)" },
    { label: "Scopes", value: `${state.scopes.length} ausgewählt` },
    { label: "Board Lead", value: state.isBoardLead ? "ja" : "nein" },
  ];

  if (!createdAgentId) {
    return (
      <div className="space-y-4">
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-6 gap-y-2">
          {rows.map((r) => (
            <div key={r.label} className="flex justify-between gap-3 py-1 border-b" style={{ borderColor: C.borderSubtle }}>
              <span className="text-[11px] uppercase tracking-wider text-[var(--color-text-muted)]">{r.label}</span>
              <span className="text-sm text-[var(--color-text-primary)] text-right truncate">{r.value}</span>
            </div>
          ))}
        </div>
        <div className="flex justify-end">
          <button
            onClick={handleCreate}
            disabled={creating}
            className="px-5 py-2.5 text-sm rounded-xl font-medium text-white disabled:opacity-40 cursor-pointer transition-all"
            style={wizardBtnPrimaryStyle}
          >
            {creating ? (
              <span className="flex items-center gap-2"><Loader2 size={14} className="animate-spin" /> Erstelle…</span>
            ) : (
              "Agent erstellen"
            )}
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {createdToken && (
        <div>
          <label className={wizardLabelClass}>Agent-Token (nur einmal sichtbar — jetzt sichern!)</label>
          <TokenDisplay token={createdToken} />
        </div>
      )}

      <div>
        <label className={wizardLabelClass}>Readiness-Check</label>
        <div className="space-y-1.5">
          {(readiness?.checks ?? [{ label: "warte auf Provisionierung…", ok: false, detail: "" }]).map((c) => (
            <div key={c.label} className="flex items-center gap-2 text-sm">
              {c.ok ? (
                <CheckCircle2 size={15} className="text-[var(--color-online)]" />
              ) : (
                <XCircle size={15} style={{ color: C.warning }} />
              )}
              <span className="text-[var(--color-text-primary)]">{c.label}</span>
              <span className="text-[11px] text-[var(--color-text-muted)]">{c.detail}</span>
            </div>
          ))}
        </div>
        {state.agentRuntime === "host" && (
          <p className="mt-2 text-[10px] text-[var(--color-text-muted)]">
            Host-Agent: Dateien wurden nach ~/.mc/agents/&lt;slug&gt;/ gerendert. Auf dem
            Mac via launchctl laden (siehe Activity-Event), dann wird der Heartbeat grün.
          </p>
        )}
      </div>

      <div className="flex justify-end">
        <button
          onClick={onCreated}
          className="px-5 py-2.5 text-sm rounded-xl font-medium text-white cursor-pointer"
          style={wizardBtnPrimaryStyle}
        >
          Fertig
        </button>
      </div>
    </div>
  );
}
