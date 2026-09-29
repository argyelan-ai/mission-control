"use client";

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2, RotateCcw } from "lucide-react";
import { api } from "@/lib/api";
import { notify } from "@/lib/notify";
import { C } from "@/lib/colors";
import { cn } from "@/lib/utils";
import type { Agent } from "@/lib/types";

/**
 * Per-agent provider key (PATCH /agents/{id} { secret_id }, applied via
 * sync-config?restart=true).
 *
 * The bound key is sent as OPENAI_API_KEY for every runtime that does not
 * sign in on its own (backend harness_compat.resolve_provider_credentials).
 * Which secret provider fits comes from the server on each runtime row
 * (`agent_key_used` / `agent_key_provider`); for an unknown vendor any LLM
 * provider key is offered — never other agents' MC tokens or unrelated
 * service keys.
 *
 * The select never shows a value that is not saved: the saved key always has
 * its own option (marked when it does not fit the runtime), so the browser
 * cannot fall back to displaying the first entry.
 */
/** `secrets.provider` values of keys that authenticate an LLM endpoint. */
const LLM_KEY_PROVIDERS = new Set(["openai", "ollama", "google", "openrouter"]);

/**
 * A key that can sensibly be sent as OPENAI_API_KEY: a known LLM provider
 * key, or a custom key the operator stored without a provider (e.g. for a
 * self-hosted server). Never MC agent tokens, OAuth tokens, chat, social,
 * git or embeddings keys.
 */
function isLlmProviderKey(s: { provider: string | null; key: string }): boolean {
  if (s.provider) return LLM_KEY_PROVIDERS.has(s.provider);
  return !s.key.startsWith("mc_agent_token");
}

export function AgentApiKeySection({ agent, agentId }: { agent: Agent; agentId: string }) {
  const t = useTranslations("agents.detail");
  const qc = useQueryClient();

  const { data: secrets } = useQuery({
    queryKey: ["secrets"],
    queryFn: () => api.secrets.list(),
  });
  const { data: runtimesData, isError: runtimesError } = useQuery({
    queryKey: ["runtimes"],
    queryFn: () => api.runtimes.list(),
  });

  const savedId = agent.secret_id ?? null;
  const [selectedSecretId, setSelectedSecretId] = useState<string | null>(savedId);
  // Follow the saved value when the agent refetches (after save, or a change
  // made elsewhere) — the select must mirror what is stored.
  useEffect(() => {
    setSelectedSecretId(savedId);
  }, [savedId]);
  const secretDirty = selectedSecretId !== savedId;

  const runtime = runtimesData?.runtimes.find(
    (r) => r.id === agent.runtime_id || r.slug === agent.runtime_id,
  );
  // Which keys to offer:
  // - known vendor → only that vendor's keys;
  // - runtime signs in on its own → none;
  // - openai-protocol runtime of an unknown vendor (local vLLM with auth,
  //   OpenRouter, a custom gateway), no runtime bound, or a runtime missing
  //   from the list → any LLM provider key. The backend sends the agent key
  //   in all these cases (it wins over the runtime's own key).
  const runtimeKnown = !!runtime;
  const keyProvider = runtime?.agent_key_used ? runtime.agent_key_provider ?? null : null;
  const anyLlmKey = runtimeKnown ? runtime.agent_key_used !== false && !keyProvider : !!runtimesData || runtimesError || !agent.runtime_id;
  const fitting = (secrets ?? []).filter((s) =>
    keyProvider ? s.provider === keyProvider : anyLlmKey ? isLlmProviderKey(s) : false,
  );

  const hint = !runtimesData
    ? runtimesError
      ? t("apiKeyHintNoRuntime")
      : null
    : !runtime
      ? t("apiKeyHintNoRuntime")
      : runtime.agent_key_used === false
        ? t("apiKeyHintOwnSignIn")
        : keyProvider
          ? t("apiKeyHintProvider", { provider: keyProvider })
          : t("apiKeyHintAnyLlmKey");

  // Options that must exist so the select can never display a value that is
  // not the stored/selected one.
  const extraIds = [savedId, selectedSecretId].filter(
    (id, i, arr): id is string => !!id && arr.indexOf(id) === i && !fitting.some((s) => s.id === id),
  );

  const updateSecretMutation = useMutation({
    mutationFn: (secret_id: string | null) =>
      api.agents.update(agentId, { secret_id } as Partial<Agent>),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["agent", agentId] });
      notify.success(t("apiKeySaved"));
    },
    onError: (e: Error) => notify.error(t("saveFailedMsg", { msg: e.message })),
  });

  const applyRestartMutation = useMutation({
    mutationFn: () => api.agents.syncConfig(agentId, { restart: true }),
    onSuccess: (result) => {
      const restartStatus = result.restart?.status ?? t("noRestart");
      notify.success(t("configSyncedPlus", { status: restartStatus }));
      qc.invalidateQueries({ queryKey: ["agent", agentId] });
    },
    onError: (e: Error) => notify.error(t("syncFailedMsg", { msg: e.message })),
  });

  const handleSaveAndApply = async () => {
    await updateSecretMutation.mutateAsync(selectedSecretId);
    await applyRestartMutation.mutateAsync();
  };

  const labelId = `agent-api-key-${agentId}`;

  return (
    <div
      className="rounded-xl p-4"
      style={{
        backgroundColor: "var(--color-bg-surface)",
        border: "1px solid var(--color-border)",
      }}
    >
      <div className="flex items-start justify-between gap-4">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 mb-2">
            <span id={labelId} className="text-xs font-mono text-[var(--color-text-muted)]">
              {t("apiKeyLabel")}
            </span>
          </div>
          <select
            aria-labelledby={labelId}
            value={selectedSecretId ?? ""}
            onChange={(e) => setSelectedSecretId(e.target.value === "" ? null : e.target.value)}
            className="w-full text-sm rounded-lg px-3 py-2 outline-none cursor-pointer"
            style={{
              backgroundColor: "var(--color-bg-elevated)",
              border: `1px solid ${secretDirty ? C.borderAccent : "var(--color-border)"}`,
              color: "var(--color-text-primary)",
            }}
          >
            <option value="">{t("apiKeyNone")}</option>
            {fitting.map((s) => (
              <option key={s.id} value={s.id}>
                {s.label ?? s.key}
              </option>
            ))}
            {extraIds.map((id) => {
              const s = secrets?.find((x) => x.id === id);
              const name = s ? s.label ?? s.key : t("apiKeyUnknownSaved");
              return (
                <option key={id} value={id}>
                  {id === savedId ? t("apiKeySavedNotFitting", { name }) : name}
                </option>
              );
            })}
          </select>
          {hint && (
            <div className="text-[10px] text-[var(--color-text-muted)] mt-1.5">{hint}</div>
          )}
        </div>
        <div className="flex flex-col gap-2 pt-[22px]">
          <button
            onClick={() => updateSecretMutation.mutate(selectedSecretId)}
            disabled={!secretDirty || updateSecretMutation.isPending}
            className={cn(
              "text-xs px-3 py-2 rounded-lg whitespace-nowrap transition-all",
              !secretDirty || updateSecretMutation.isPending
                ? "cursor-not-allowed opacity-40"
                : "cursor-pointer",
            )}
            style={{
              backgroundColor: "var(--color-bg-elevated)",
              border: "1px solid var(--color-border)",
              color: "var(--color-text-secondary)",
            }}
          >
            {updateSecretMutation.isPending ? t("savingEllipsis") : t("save")}
          </button>
          <button
            onClick={handleSaveAndApply}
            disabled={applyRestartMutation.isPending || updateSecretMutation.isPending}
            className="flex items-center gap-1.5 text-xs px-3 py-2 rounded-lg whitespace-nowrap cursor-pointer"
            style={{ backgroundColor: C.accent, color: C.onAccent }}
          >
            {applyRestartMutation.isPending ? (
              <Loader2 size={12} className="animate-spin" />
            ) : (
              <RotateCcw size={12} />
            )}
            {t("applyRestart")}
          </button>
        </div>
      </div>
    </div>
  );
}
