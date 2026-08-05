"use client";

import { useState } from "react";
import { motion } from "framer-motion";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { Eye, EyeOff, Loader2 } from "lucide-react";
import { api } from "@/lib/api";
import { notify } from "@/lib/notify";
import type {
  ChannelSettingsResponse,
  SecretEntry,
  TelegramBotStatus,
  TelegramConnectionResult,
} from "@/lib/types";
import { C, STATUS_TEXT } from "@/lib/colors";
import { StatusDot } from "@/components/shared/StatusDot";

// Key names match the backend catalogs (routers/secrets.py PROVIDER_TEMPLATES
// and services/channel_config.CHANNEL_SETTING_FIELDS).
const COMMAND_TOKEN_KEY = "telegram_bot_token";
const REPORTS_TOKEN_KEY = "telegram_reports_bot_token";

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

// ── Toggle row (shared vocabulary with the Slack card) ────────────────────

export function ChannelToggleRow({
  label,
  hint,
  value,
  onChange,
  saving,
  testId,
}: {
  label: string;
  hint: string;
  value: boolean;
  onChange: (next: boolean) => void;
  saving: boolean;
  testId: string;
}) {
  return (
    <div className="flex items-center justify-between gap-4 py-1.5">
      <div className="flex-1 min-w-0">
        <div className="text-sm" style={{ color: "var(--color-text-primary)" }}>
          {label}
        </div>
        <p className="text-xs mt-0.5" style={{ color: "var(--color-text-muted)" }}>
          {hint}
        </p>
      </div>
      <button
        type="button"
        role="switch"
        aria-checked={value}
        aria-label={label}
        data-testid={testId}
        disabled={saving}
        onClick={() => onChange(!value)}
        className="shrink-0 relative rounded-full cursor-pointer transition-colors disabled:opacity-50"
        style={{
          width: 36,
          height: 20,
          backgroundColor: value ? C.accent : "var(--color-bg-elevated)",
          border: `1px solid ${value ? C.accent : "var(--color-border)"}`,
        }}
      >
        <span
          className="absolute top-1/2 -translate-y-1/2 rounded-full transition-all"
          style={{
            left: value ? 18 : 2,
            width: 14,
            height: 14,
            backgroundColor: value ? "var(--color-on-accent)" : "var(--color-text-muted)",
          }}
        />
      </button>
    </div>
  );
}

// ── Text setting row (chat ids / channel names) ───────────────────────────

function SettingTextField({
  label,
  hint,
  value,
  placeholder,
  onSave,
  saving,
  mono = true,
}: {
  label: string;
  hint: string;
  value: string;
  placeholder: string;
  onSave: (value: string) => void;
  saving: boolean;
  mono?: boolean;
}) {
  const t = useTranslations("settings.telegram");
  const [draft, setDraft] = useState<string | null>(null);
  const shown = draft ?? value;
  const dirty = draft !== null && draft !== value;

  return (
    <div>
      <FieldLabel>{label}</FieldLabel>
      <div className="flex gap-2">
        <input
          type="text"
          value={shown}
          onChange={(e) => setDraft(e.target.value)}
          placeholder={placeholder}
          aria-label={label}
          autoComplete="off"
          spellCheck={false}
          className={`flex-1 rounded-lg px-3 py-2.5 text-sm outline-none transition-all duration-200 ${mono ? "font-mono" : ""}`}
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

// ── Token field (same shape as the Slack card's) ──────────────────────────

function TokenField({
  label,
  secretKey,
  hint,
  existing,
  onSave,
  saving,
}: {
  label: string;
  secretKey: string;
  hint: string;
  existing: SecretEntry | undefined;
  onSave: (value: string) => void;
  saving: boolean;
}) {
  const t = useTranslations("settings.telegram");
  const [value, setValue] = useState("");
  const [reveal, setReveal] = useState(false);
  const inputId = `telegram-${secretKey}`;

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
            placeholder={existing ? t("pasteToReplace", { masked: existing.value_masked }) : "1234567890:AA..."}
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

// ── Bot status line ───────────────────────────────────────────────────────

function BotStatusLine({ label, status }: { label: string; status: TelegramBotStatus | undefined }) {
  const t = useTranslations("settings.telegram");
  const dot: "online" | "warning" | "error" | "idle" = !status || !status.token_set
    ? "idle"
    : status.connected
    ? status.chat_id_set
      ? "online"
      : "warning"
    : "error";
  const text = !status || !status.token_set
    ? t("botNotSetUp")
    : status.connected
    ? status.chat_id_set
      ? t("botConnected", { username: status.bot_username ?? "?" })
      : t("botConnectedNoChat", { username: status.bot_username ?? "?" })
    : t("botNotConnected");

  return (
    <div className="space-y-1">
      <div className="flex items-center gap-2.5">
        <StatusDot status={dot} size="lg" />
        <span className="text-xs" style={{ color: "var(--color-text-muted)" }}>{label}</span>
        <span className="text-sm font-medium" style={{ color: "var(--color-text-primary)" }}>
          {text}
        </span>
      </div>
      {status?.error && (
        <p className="text-xs pl-6" style={{ color: STATUS_TEXT.error }}>
          {status.error}
        </p>
      )}
    </div>
  );
}

// ── Tab ───────────────────────────────────────────────────────────────────

export function TelegramTab() {
  const t = useTranslations("settings.telegram");
  const queryClient = useQueryClient();
  const [savingKey, setSavingKey] = useState<string | null>(null);
  const [savingSetting, setSavingSetting] = useState<string | null>(null);

  const { data: secrets } = useQuery<SecretEntry[]>({
    queryKey: ["secrets"],
    queryFn: () => api.secrets.list(),
  });

  const { data: channelSettings } = useQuery<ChannelSettingsResponse>({
    queryKey: ["channel-settings"],
    queryFn: () => api.channels.getSettings(),
  });

  const {
    data: status,
    isFetching: testing,
    refetch,
  } = useQuery<TelegramConnectionResult>({
    queryKey: ["telegram-connection"],
    queryFn: () => api.channels.telegramTestConnection(),
    staleTime: 60_000,
    refetchOnWindowFocus: false,
    retry: false,
  });

  const saveToken = useMutation({
    mutationFn: async ({ key, value }: { key: string; value: string }) => {
      const exists = (secrets ?? []).some((s) => s.key === key);
      return exists
        ? api.secrets.update(key, { value })
        : api.secrets.create({ key, value, provider: "telegram" });
    },
    onMutate: ({ key }) => setSavingKey(key),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["secrets"] });
      notify.success(t("tokenSaved"));
      await refetch();
    },
    onError: () => notify.error(t("tokenSaveFailed")),
    onSettled: () => setSavingKey(null),
  });

  const saveSetting = useMutation({
    mutationFn: ({ key, value }: { key: string; value: string | boolean }) =>
      api.channels.updateSettings({ [key]: value }),
    onMutate: ({ key }) => setSavingSetting(key),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["channel-settings"] });
      notify.success(t("settingSaved"));
    },
    onError: () => notify.error(t("settingSaveFailed")),
    onSettled: () => setSavingSetting(null),
  });

  const byKey = new Map((secrets ?? []).map((s) => [s.key, s]));
  const values = channelSettings?.values ?? {};
  const boolOf = (key: string, fallback: boolean) =>
    typeof values[key] === "boolean" ? (values[key] as boolean) : fallback;
  const strOf = (key: string) => (typeof values[key] === "string" ? (values[key] as string) : "");

  const toggles: { key: string; labelKey: string; hintKey: string }[] = [
    { key: "telegram_reports_enabled", labelKey: "toggleReports", hintKey: "toggleReportsHint" },
    { key: "telegram_approvals_enabled", labelKey: "toggleApprovals", hintKey: "toggleApprovalsHint" },
    { key: "telegram_team_chat_enabled", labelKey: "toggleTeamChat", hintKey: "toggleTeamChatHint" },
    { key: "jarvis_telegram_enabled", labelKey: "toggleJarvis", hintKey: "toggleJarvisHint" },
  ];

  return (
    <motion.div
      key="telegram"
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
        {/* Status — both bots at a glance */}
        <div className="mc-card p-4 space-y-2.5" style={cardStyle}>
          <BotStatusLine label={t("commandBot")} status={status?.command_bot} />
          <BotStatusLine label={t("reportsBot")} status={status?.reports_bot} />
          <button
            onClick={() => refetch()}
            disabled={testing}
            className="mt-1 text-xs px-2.5 py-1.5 rounded-lg cursor-pointer disabled:opacity-50 transition-all"
            style={{ background: "var(--color-bg-elevated)", color: "var(--color-text-secondary)" }}
          >
            {testing ? t("testing") : t("testConnection")}
          </button>
        </div>

        {/* Tokens + targets */}
        <div className="mc-card p-4 space-y-4" style={cardStyle}>
          <TokenField
            label={t("commandTokenLabel")}
            secretKey={COMMAND_TOKEN_KEY}
            hint={t("commandTokenHint")}
            existing={byKey.get(COMMAND_TOKEN_KEY)}
            saving={savingKey === COMMAND_TOKEN_KEY}
            onSave={(value) => saveToken.mutate({ key: COMMAND_TOKEN_KEY, value })}
          />
          <SettingTextField
            label={t("chatIdLabel")}
            hint={t("chatIdHint")}
            value={strOf("telegram_chat_id")}
            placeholder="6274…"
            saving={savingSetting === "telegram_chat_id"}
            onSave={(value) => saveSetting.mutate({ key: "telegram_chat_id", value })}
          />
          <TokenField
            label={t("reportsTokenLabel")}
            secretKey={REPORTS_TOKEN_KEY}
            hint={t("reportsTokenHint")}
            existing={byKey.get(REPORTS_TOKEN_KEY)}
            saving={savingKey === REPORTS_TOKEN_KEY}
            onSave={(value) => saveToken.mutate({ key: REPORTS_TOKEN_KEY, value })}
          />
          <SettingTextField
            label={t("reportsChatIdLabel")}
            hint={t("reportsChatIdHint")}
            value={strOf("telegram_reports_chat_id")}
            placeholder="6274…"
            saving={savingSetting === "telegram_reports_chat_id"}
            onSave={(value) => saveSetting.mutate({ key: "telegram_reports_chat_id", value })}
          />
        </div>

        {/* Per-function toggles */}
        <div className="mc-card p-4" style={cardStyle}>
          <div className="text-sm font-medium mb-2" style={{ color: "var(--color-text-primary)" }}>
            {t("togglesTitle")}
          </div>
          <p className="text-xs mb-2" style={{ color: "var(--color-text-muted)", maxWidth: "72ch" }}>
            {t("togglesHint")}
          </p>
          {toggles.map((row) => (
            <ChannelToggleRow
              key={row.key}
              label={t(row.labelKey)}
              hint={t(row.hintKey)}
              value={boolOf(row.key, true)}
              saving={savingSetting === row.key}
              testId={`toggle-${row.key}`}
              onChange={(next) => saveSetting.mutate({ key: row.key, value: next })}
            />
          ))}
        </div>

        <p className="text-xs" style={{ color: "var(--color-text-muted)", maxWidth: "72ch" }}>
          {t("docsNote")}
        </p>
      </div>
    </motion.div>
  );
}
