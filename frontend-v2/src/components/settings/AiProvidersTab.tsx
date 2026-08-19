"use client";

import { useState } from "react";
import { motion } from "framer-motion";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { Eye, EyeOff, Loader2 } from "lucide-react";
import { api } from "@/lib/api";
import { notify } from "@/lib/notify";
import type {
  AiProviderSettingsResponse,
  EmbeddingsConnectionResult,
  HfConnectionResult,
  SecretEntry,
} from "@/lib/types";
import { C, STATUS_TEXT } from "@/lib/colors";
import { StatusDot } from "@/components/shared/StatusDot";

// Key names match the backend catalogs (routers/secrets.py PROVIDER_TEMPLATES
// and services/ai_provider_config.AI_PROVIDER_SETTING_FIELDS).
const HF_TOKEN_KEY = "hf_token";
const OLLAMA_KEY = "ollama_api_key";
const EMB_KEY = "embeddings_api_key";
const EMB_CLOUD_KEY = "embeddings_cloud_api_key";

const SECRET_PROVIDER: Record<string, string> = {
  [HF_TOKEN_KEY]: "huggingface",
  [OLLAMA_KEY]: "ollama",
  [EMB_KEY]: "embeddings",
  [EMB_CLOUD_KEY]: "embeddings",
};

const cardStyle = {
  background: C.bgSurface,
  border: `1px solid ${C.border}`,
  borderRadius: 12,
} as const;

function FieldLabel({ children }: { children: React.ReactNode }) {
  return (
    <span
      className="text-xs font-medium uppercase tracking-widest block mb-1.5"
      style={{ color: "var(--color-text-secondary)" }}
    >
      {children}
    </span>
  );
}

/** "überschrieben" — this value comes from the settings page, not from .env.
 *  Without it the page cannot answer "why is it set to that?". */
function OverriddenBadge({ shown }: { shown: boolean }) {
  const t = useTranslations("settings.aiProviders");
  if (!shown) return null;
  return (
    <span
      data-testid="overridden-badge"
      className="ml-2 text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded"
      style={{ background: "var(--color-bg-elevated)", color: "var(--color-text-muted)" }}
    >
      {t("overridden")}
    </span>
  );
}

// ── Provider select (one per function) ────────────────────────────────────

function ProviderSelect({
  label,
  hint,
  value,
  choices,
  overridden,
  onChange,
  saving,
  testId,
}: {
  label: string;
  hint: string;
  value: string;
  choices: string[];
  overridden: boolean;
  onChange: (next: string) => void;
  saving: boolean;
  testId: string;
}) {
  const t = useTranslations("settings.aiProviders");
  return (
    <div>
      <label htmlFor={testId}>
        <FieldLabel>
          {label}
          <OverriddenBadge shown={overridden} />
        </FieldLabel>
      </label>
      <select
        id={testId}
        data-testid={testId}
        value={value}
        disabled={saving}
        aria-label={label}
        onChange={(e) => onChange(e.target.value)}
        className="w-full rounded-lg px-3 py-2.5 text-sm outline-none cursor-pointer disabled:opacity-50"
        style={{
          backgroundColor: C.bgDeep,
          borderWidth: 1,
          borderStyle: "solid",
          borderColor: "var(--color-border)",
          color: "var(--color-text-primary)",
        }}
      >
        {choices.map((choice) => (
          <option key={choice} value={choice}>
            {t(`provider.${choice}`)}
          </option>
        ))}
      </select>
      <p className="text-xs mt-1.5" style={{ color: "var(--color-text-muted)" }}>
        {hint}
      </p>
    </div>
  );
}

// ── Optional text override (url / model) ──────────────────────────────────

function OverrideTextField({
  label,
  hint,
  value,
  placeholder,
  overridden,
  onSave,
  saving,
  testId,
}: {
  label: string;
  hint: string;
  value: string;
  placeholder: string;
  overridden: boolean;
  onSave: (value: string) => void;
  saving: boolean;
  testId: string;
}) {
  const t = useTranslations("settings.aiProviders");
  const [draft, setDraft] = useState<string | null>(null);
  const shown = draft ?? value;
  const dirty = draft !== null && draft !== value;

  return (
    <div>
      <label htmlFor={testId}>
        <FieldLabel>
          {label}
          <OverriddenBadge shown={overridden} />
        </FieldLabel>
      </label>
      <div className="flex gap-2">
        <input
          id={testId}
          data-testid={testId}
          type="text"
          value={shown}
          onChange={(e) => setDraft(e.target.value)}
          placeholder={placeholder}
          aria-label={label}
          autoComplete="off"
          spellCheck={false}
          className="flex-1 rounded-lg px-3 py-2.5 text-sm outline-none transition-all duration-200 font-mono"
          style={{
            backgroundColor: C.bgDeep,
            borderWidth: 1,
            borderStyle: "solid",
            borderColor: "var(--color-border)",
            color: "var(--color-text-primary)",
          }}
          onFocus={(e) => { e.currentTarget.style.borderColor = C.borderAccent; }}
          onBlur={(e) => { e.currentTarget.style.borderColor = "var(--color-border)"; }}
        />
        <button
          onClick={() => { onSave((draft ?? "").trim()); setDraft(null); }}
          disabled={!dirty || saving}
          className="shrink-0 px-3 py-2 rounded-lg text-xs font-medium cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed text-[var(--color-on-accent)]"
          style={{ backgroundColor: C.accent }}
        >
          {saving ? <Loader2 size={12} className="animate-spin" /> : t("save")}
        </button>
      </div>
      <p className="text-xs mt-1.5" style={{ color: "var(--color-text-muted)" }}>
        {hint}
      </p>
    </div>
  );
}

// ── Token field (same shape as the Telegram/Slack cards') ─────────────────

function TokenField({
  label,
  secretKey,
  hint,
  placeholder,
  existing,
  onSave,
  saving,
}: {
  label: string;
  secretKey: string;
  hint: string;
  placeholder: string;
  existing: SecretEntry | undefined;
  onSave: (value: string) => void;
  saving: boolean;
}) {
  const t = useTranslations("settings.aiProviders");
  const [value, setValue] = useState("");
  const [reveal, setReveal] = useState(false);
  const inputId = `ai-${secretKey}`;

  return (
    <div>
      <label htmlFor={inputId}>
        <FieldLabel>{label}</FieldLabel>
      </label>
      <div className="flex gap-2">
        <div className="relative flex-1">
          <input
            id={inputId}
            type={reveal ? "text" : "password"}
            value={value}
            onChange={(e) => setValue(e.target.value)}
            placeholder={existing ? t("pasteToReplace", { masked: existing.value_masked }) : placeholder}
            aria-label={label}
            autoComplete="off"
            spellCheck={false}
            className="w-full rounded-lg px-3 py-2.5 pr-10 text-sm outline-none transition-all duration-200 font-mono"
            style={{
              backgroundColor: C.bgDeep,
              borderWidth: 1,
              borderStyle: "solid",
              borderColor: "var(--color-border)",
              color: "var(--color-text-primary)",
            }}
            onFocus={(e) => { e.currentTarget.style.borderColor = C.borderAccent; }}
            onBlur={(e) => { e.currentTarget.style.borderColor = "var(--color-border)"; }}
          />
          <button
            type="button"
            onClick={() => setReveal((r) => !r)}
            aria-label={reveal ? t("hideLabel", { label }) : t("showLabel", { label })}
            className="absolute right-3 top-1/2 -translate-y-1/2 cursor-pointer"
            style={{ color: "var(--color-text-muted)" }}
          >
            {reveal ? <EyeOff size={14} /> : <Eye size={14} />}
          </button>
        </div>
        <button
          onClick={() => { onSave(value.trim()); setValue(""); setReveal(false); }}
          disabled={!value.trim() || saving}
          className="shrink-0 px-3 py-2 rounded-lg text-xs font-medium cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed text-[var(--color-on-accent)]"
          style={{ backgroundColor: C.accent }}
        >
          {saving ? <Loader2 size={12} className="animate-spin" /> : t("save")}
        </button>
      </div>
      <p className="text-xs mt-1.5" style={{ color: "var(--color-text-muted)" }}>
        {hint}
      </p>
    </div>
  );
}

// ── Status lines ──────────────────────────────────────────────────────────

function EmbeddingsStatusLine({ status }: { status: EmbeddingsConnectionResult | undefined }) {
  const t = useTranslations("settings.aiProviders");
  const dot: "online" | "warning" | "error" | "idle" = !status
    ? "idle"
    : status.connected
    ? status.error
      ? "warning"
      : "online"
    : "error";
  const text = !status
    ? t("notTested")
    : status.connected
    ? t("embeddingsOk", { label: status.label, dimension: status.dimension ?? 0 })
    : t("embeddingsDown", { label: status.label });

  return (
    <div className="space-y-1">
      <div className="flex items-center gap-2.5">
        <StatusDot status={dot} size="lg" />
        <span className="text-xs" style={{ color: "var(--color-text-muted)" }}>
          {t("embeddings")}
        </span>
        <span className="text-sm font-medium" style={{ color: "var(--color-text-primary)" }}>
          {text}
        </span>
      </div>
      {status?.model && (
        <p className="text-xs pl-6 font-mono" style={{ color: "var(--color-text-muted)" }}>
          {status.model}
        </p>
      )}
      {status?.error && (
        <p
          data-testid="embeddings-error"
          className="text-xs pl-6"
          style={{ color: STATUS_TEXT.error }}
        >
          {status.error}
        </p>
      )}
    </div>
  );
}

function HfStatusLine({ status }: { status: HfConnectionResult | undefined }) {
  const t = useTranslations("settings.aiProviders");
  const dot: "online" | "warning" | "error" | "idle" = !status
    ? "idle"
    : !status.token_set
    ? "idle"
    : status.connected
    ? "online"
    : "error";
  const text = !status
    ? t("notTested")
    : !status.token_set
    ? t("hfAnonymous")
    : status.connected
    ? t("hfConnected", { username: status.username ?? "?" })
    : t("hfRejected");

  return (
    <div className="space-y-1">
      <div className="flex items-center gap-2.5">
        <StatusDot status={dot} size="lg" />
        <span className="text-xs" style={{ color: "var(--color-text-muted)" }}>
          {t("huggingface")}
        </span>
        <span className="text-sm font-medium" style={{ color: "var(--color-text-primary)" }}>
          {text}
        </span>
      </div>
      {status?.error && (
        <p
          data-testid="hf-error"
          className="text-xs pl-6"
          style={{ color: STATUS_TEXT.error }}
        >
          {status.error}
        </p>
      )}
    </div>
  );
}

// ── Tab ───────────────────────────────────────────────────────────────────

export function AiProvidersTab() {
  const t = useTranslations("settings.aiProviders");
  const queryClient = useQueryClient();
  const [savingKey, setSavingKey] = useState<string | null>(null);
  const [savingSetting, setSavingSetting] = useState<string | null>(null);

  const { data: secrets } = useQuery<SecretEntry[]>({
    queryKey: ["secrets"],
    queryFn: () => api.secrets.list(),
  });

  const { data: providerSettings } = useQuery<AiProviderSettingsResponse>({
    queryKey: ["ai-provider-settings"],
    queryFn: () => api.aiProviders.getSettings(),
  });

  const {
    data: hfStatus,
    isFetching: testingHf,
    refetch: refetchHf,
  } = useQuery<HfConnectionResult>({
    queryKey: ["hf-connection"],
    queryFn: () => api.aiProviders.huggingfaceTestConnection(),
    staleTime: 60_000,
    refetchOnWindowFocus: false,
    retry: false,
  });

  // Embeddings are NOT probed on open: the test sends a real embed request to
  // the GPU box, and opening a settings page must not wake hardware.
  const {
    data: embStatus,
    isFetching: testingEmb,
    refetch: refetchEmb,
  } = useQuery<EmbeddingsConnectionResult>({
    queryKey: ["embeddings-connection"],
    queryFn: () => api.aiProviders.embeddingsTestConnection(),
    enabled: false,
    retry: false,
  });

  const saveToken = useMutation({
    mutationFn: async ({ key, value }: { key: string; value: string }) => {
      const exists = (secrets ?? []).some((s) => s.key === key);
      return exists
        ? api.secrets.update(key, { value })
        : api.secrets.create({
            key,
            value,
            provider: SECRET_PROVIDER[key] ?? "ollama",
          });
    },
    onMutate: ({ key }) => setSavingKey(key),
    onSuccess: async (_data, { key }) => {
      await queryClient.invalidateQueries({ queryKey: ["secrets"] });
      await queryClient.invalidateQueries({ queryKey: ["ai-provider-settings"] });
      notify.success(t("tokenSaved"));
      if (key === HF_TOKEN_KEY) await refetchHf();
    },
    onError: () => notify.error(t("tokenSaveFailed")),
    onSettled: () => setSavingKey(null),
  });

  const saveSetting = useMutation({
    mutationFn: ({ key, value }: { key: string; value: string }) =>
      api.aiProviders.updateSettings({ [key]: value }),
    onMutate: ({ key }) => setSavingSetting(key),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["ai-provider-settings"] });
      notify.success(t("settingSaved"));
    },
    onError: () => notify.error(t("settingSaveFailed")),
    onSettled: () => setSavingSetting(null),
  });

  const byKey = new Map((secrets ?? []).map((s) => [s.key, s]));
  const values = providerSettings?.values ?? {};
  const overridden = new Set(providerSettings?.overridden ?? []);
  const strOf = (key: string, fallback = "") =>
    typeof values[key] === "string" && values[key] !== null
      ? (values[key] as string)
      : fallback;

  const embeddingChoices = providerSettings?.choices.ai_embeddings_provider ?? [];
  const insightsChoices = providerSettings?.choices.ai_insights_provider ?? [];
  const needsOllamaKey =
    (providerSettings?.state.ollama_key_required ?? false) &&
    !(providerSettings?.state.ollama_api_key_set ?? false);
  const activeEmbProvider = strOf("ai_embeddings_provider", "spark");
  const needsCloudEmbKey =
    (providerSettings?.state.embeddings_cloud_key_required ?? false) &&
    !(providerSettings?.state.embeddings_cloud_api_key_set ?? false);

  return (
    <motion.div
      key="ai-providers"
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: -4 }}
      transition={{ duration: 0.3, ease: [0.16, 1, 0.3, 1] }}
    >
      <div className="mb-6">
        <h2 className="text-base font-semibold" style={{ color: "var(--color-text-primary)" }}>
          {t("title")}
        </h2>
        <p className="text-sm mt-1" style={{ color: "var(--color-text-muted)", maxWidth: "72ch" }}>
          {t("intro")}
        </p>
      </div>

      <div className="space-y-4">
        {/* Status — what MC's own AI is doing right now */}
        <div className="mc-card p-4 space-y-2.5" style={cardStyle}>
          <EmbeddingsStatusLine status={embStatus} />
          <HfStatusLine status={hfStatus} />
          <div className="flex gap-2 pt-1">
            <button
              onClick={() => refetchEmb()}
              disabled={testingEmb}
              className="text-xs px-2.5 py-1.5 rounded-lg cursor-pointer disabled:opacity-50 transition-all"
              style={{ background: "var(--color-bg-elevated)", color: "var(--color-text-secondary)" }}
            >
              {testingEmb ? t("testing") : t("testEmbeddings")}
            </button>
            <button
              onClick={() => refetchHf()}
              disabled={testingHf}
              className="text-xs px-2.5 py-1.5 rounded-lg cursor-pointer disabled:opacity-50 transition-all"
              style={{ background: "var(--color-bg-elevated)", color: "var(--color-text-secondary)" }}
            >
              {testingHf ? t("testing") : t("testHuggingface")}
            </button>
          </div>
        </div>

        {/* Per-function routing. Rendered only once the effective config has
            arrived: a provider select that shows "spark" before it knows what
            is configured is a claim we cannot back up yet, and its option list
            would still be empty. */}
        <div className="mc-card p-4 space-y-4" style={cardStyle}>
          <div>
            <div className="text-sm font-medium" style={{ color: "var(--color-text-primary)" }}>
              {t("routingTitle")}
            </div>
            <p className="text-xs mt-0.5" style={{ color: "var(--color-text-muted)", maxWidth: "72ch" }}>
              {t("routingHint")}
            </p>
          </div>

          {!providerSettings ? (
            <div
              data-testid="routing-loading"
              className="flex items-center gap-2 text-xs py-2"
              style={{ color: "var(--color-text-muted)" }}
            >
              <Loader2 size={12} className="animate-spin" />
              {t("loading")}
            </div>
          ) : (
          <>
          <ProviderSelect
            label={t("embeddingsProviderLabel")}
            hint={t("embeddingsProviderHint")}
            value={activeEmbProvider}
            choices={embeddingChoices}
            overridden={overridden.has("ai_embeddings_provider")}
            saving={savingSetting === "ai_embeddings_provider"}
            testId="ai_embeddings_provider"
            onChange={(value) => saveSetting.mutate({ key: "ai_embeddings_provider", value })}
          />
          {/* Per-arm fields: each provider keeps its OWN url/model, so the
              select above is always a complete one-click switch — no field
              from the other arm can leak into the active one. */}
          {activeEmbProvider === "cloud" ? (
            <>
              <OverrideTextField
                label={t("embeddingsCloudUrlLabel")}
                hint={t("embeddingsCloudUrlHint")}
                value={strOf("ai_embeddings_cloud_url")}
                placeholder="https://api.together.xyz/v1/embeddings"
                overridden={overridden.has("ai_embeddings_cloud_url")}
                saving={savingSetting === "ai_embeddings_cloud_url"}
                testId="ai_embeddings_cloud_url"
                onSave={(value) => saveSetting.mutate({ key: "ai_embeddings_cloud_url", value })}
              />
              <OverrideTextField
                label={t("embeddingsCloudModelLabel")}
                hint={t("embeddingsModelHint")}
                value={strOf("ai_embeddings_cloud_model")}
                placeholder="nomic-ai/nomic-embed-text-v1.5"
                overridden={overridden.has("ai_embeddings_cloud_model")}
                saving={savingSetting === "ai_embeddings_cloud_model"}
                testId="ai_embeddings_cloud_model"
                onSave={(value) => saveSetting.mutate({ key: "ai_embeddings_cloud_model", value })}
              />
              {needsCloudEmbKey && (
                <p
                  data-testid="cloud-emb-key-warning"
                  className="text-xs"
                  style={{ color: STATUS_TEXT.warning, maxWidth: "72ch" }}
                >
                  {t("cloudEmbKeyMissing")}
                </p>
              )}
            </>
          ) : (
            <>
              <OverrideTextField
                label={t("embeddingsUrlLabel")}
                hint={t("embeddingsUrlHint")}
                value={strOf("ai_embeddings_url")}
                placeholder={t("inheritPlaceholder")}
                overridden={overridden.has("ai_embeddings_url")}
                saving={savingSetting === "ai_embeddings_url"}
                testId="ai_embeddings_url"
                onSave={(value) => saveSetting.mutate({ key: "ai_embeddings_url", value })}
              />
              <OverrideTextField
                label={t("embeddingsModelLabel")}
                hint={t("embeddingsModelHint")}
                value={strOf("ai_embeddings_model")}
                placeholder={t("inheritPlaceholder")}
                overridden={overridden.has("ai_embeddings_model")}
                saving={savingSetting === "ai_embeddings_model"}
                testId="ai_embeddings_model"
                onSave={(value) => saveSetting.mutate({ key: "ai_embeddings_model", value })}
              />
            </>
          )}

          <div style={{ borderTop: `1px solid ${C.border}` }} className="pt-4 space-y-4">
            <ProviderSelect
              label={t("insightsProviderLabel")}
              hint={t("insightsProviderHint")}
              value={strOf("ai_insights_provider", "spark")}
              choices={insightsChoices}
              overridden={overridden.has("ai_insights_provider")}
              saving={savingSetting === "ai_insights_provider"}
              testId="ai_insights_provider"
              onChange={(value) => saveSetting.mutate({ key: "ai_insights_provider", value })}
            />
            <OverrideTextField
              label={t("insightsModelLabel")}
              hint={t("insightsModelHint")}
              value={strOf("ai_insights_model")}
              placeholder={t("inheritPlaceholder")}
              overridden={overridden.has("ai_insights_model")}
              saving={savingSetting === "ai_insights_model"}
              testId="ai_insights_model"
              onSave={(value) => saveSetting.mutate({ key: "ai_insights_model", value })}
            />
          </div>
          </>
          )}
        </div>

        {/* Keys */}
        <div className="mc-card p-4 space-y-4" style={cardStyle}>
          <div className="text-sm font-medium" style={{ color: "var(--color-text-primary)" }}>
            {t("keysTitle")}
          </div>
          <TokenField
            label={t("hfTokenLabel")}
            secretKey={HF_TOKEN_KEY}
            hint={t("hfTokenHint")}
            placeholder="hf_..."
            existing={byKey.get(HF_TOKEN_KEY)}
            saving={savingKey === HF_TOKEN_KEY}
            onSave={(value) => saveToken.mutate({ key: HF_TOKEN_KEY, value })}
          />
          <TokenField
            label={t("embKeyLabel")}
            secretKey={EMB_KEY}
            hint={t("embKeyHint")}
            placeholder={t("embKeyPlaceholder")}
            existing={byKey.get(EMB_KEY)}
            saving={savingKey === EMB_KEY}
            onSave={(value) => saveToken.mutate({ key: EMB_KEY, value })}
          />
          <TokenField
            label={t("embCloudKeyLabel")}
            secretKey={EMB_CLOUD_KEY}
            hint={t("embCloudKeyHint")}
            placeholder={t("embKeyPlaceholder")}
            existing={byKey.get(EMB_CLOUD_KEY)}
            saving={savingKey === EMB_CLOUD_KEY}
            onSave={(value) => saveToken.mutate({ key: EMB_CLOUD_KEY, value })}
          />
          <TokenField
            label={t("ollamaKeyLabel")}
            secretKey={OLLAMA_KEY}
            hint={t("ollamaKeyHint")}
            placeholder="oll-..."
            existing={byKey.get(OLLAMA_KEY)}
            saving={savingKey === OLLAMA_KEY}
            onSave={(value) => saveToken.mutate({ key: OLLAMA_KEY, value })}
          />
          {needsOllamaKey && (
            <p
              data-testid="ollama-key-warning"
              className="text-xs"
              style={{ color: STATUS_TEXT.warning, maxWidth: "72ch" }}
            >
              {t("ollamaKeyMissing")}
            </p>
          )}
        </div>

        <p className="text-xs" style={{ color: "var(--color-text-muted)", maxWidth: "72ch" }}>
          {t("docsNote")}
        </p>
      </div>
    </motion.div>
  );
}
