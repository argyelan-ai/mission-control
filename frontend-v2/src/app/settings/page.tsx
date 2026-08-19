"use client";

import { Suspense, useState, useEffect } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { useTranslations, useLocale } from "next-intl";
import Link from "next/link";
import { motion, AnimatePresence } from "framer-motion";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  User,
  Shield,
  Users,
  Key,
  KeyRound,
  Github,
  Zap,
  SlidersHorizontal,
  Keyboard,
  Info,
  Save,
  Loader2,
  Check,
  AlertCircle,
  Plus,
  Eye,
  EyeOff,
  X,
  Trash2,
  Play,
  ExternalLink,
  DollarSign,
  MessageSquare,
  Send,
  BrainCircuit,
  type LucideIcon,
} from "lucide-react";
import { api, setStoredUser } from "@/lib/api";
import { useAppStore, type AuthUser } from "@/lib/store";
import type {
  AiProviderSettingsResponse,
  IntelligenceConfig,
  ProviderTemplate,
  SecretEntry,
  GithubStatus,
  GithubConfigUpdate,
} from "@/lib/types";
import { cn } from "@/lib/utils";
import AppShell from "@/components/layout/AppShell";
import { CredentialsTab } from "@/components/settings/CredentialsTab";
import { CostPricesTab } from "@/components/settings/CostPricesTab";
import { SlackTab } from "@/components/settings/SlackTab";
import { TelegramTab } from "@/components/settings/TelegramTab";
import { AiProvidersTab } from "@/components/settings/AiProvidersTab";
import { StatusDot } from "@/components/shared/StatusDot";
import { ConfirmDialog } from "@/components/shared/ConfirmDialog";
import { C, STATUS_TEXT } from "@/lib/colors";

// ── Section Registry ──────────────────────────────────────────────────────────

// labelKey pattern (docs/i18n.md): keys resolve via t() at the render site —
// never store translated strings in module constants.
// Thirteen entries in one flat list put "change my password" next to "how much
// autonomy do agents have" next to "where does the Slack token live". The
// groups answer one question each: whose account, how the fleet behaves, what
// it talks to, what secrets it holds, who administers it.
type SettingsGroup = "account" | "fleet" | "connections" | "secrets" | "system";

const GROUP_ORDER: SettingsGroup[] = ["account", "fleet", "connections", "secrets", "system"];

interface SettingsSection {
  id: string;
  labelKey: string;
  icon: LucideIcon;
  group: SettingsGroup;
  adminOnly?: boolean;
}

const SECTIONS: SettingsSection[] = [
  { id: "profile", labelKey: "sections.profile", icon: User, group: "account" },
  { id: "security", labelKey: "sections.security", icon: Shield, group: "account" },
  { id: "shortcuts", labelKey: "sections.shortcuts", icon: Keyboard, group: "account" },
  { id: "autonomy", labelKey: "sections.autonomy", icon: SlidersHorizontal, group: "fleet", adminOnly: true },
  { id: "intelligence", labelKey: "sections.intelligence", icon: Zap, group: "fleet", adminOnly: true },
  { id: "costs", labelKey: "sections.costs", icon: DollarSign, group: "fleet", adminOnly: true },
  { id: "github", labelKey: "sections.github", icon: Github, group: "connections", adminOnly: true },
  { id: "slack", labelKey: "sections.slack", icon: MessageSquare, group: "connections", adminOnly: true },
  { id: "telegram", labelKey: "sections.telegram", icon: Send, group: "connections", adminOnly: true },
  { id: "ai-providers", labelKey: "sections.aiProviders", icon: BrainCircuit, group: "connections", adminOnly: true },
  { id: "apikeys", labelKey: "sections.apikeys", icon: Key, group: "secrets", adminOnly: true },
  { id: "credentials", labelKey: "sections.credentials", icon: KeyRound, group: "secrets", adminOnly: true },
  { id: "users", labelKey: "sections.users", icon: Users, group: "system", adminOnly: true },
  { id: "about", labelKey: "sections.about", icon: Info, group: "system" },
];

// ── Keyboard shortcuts reference ──────────────────────────────────────────────

const SHORTCUTS = [
  { keys: ["Cmd", "K"], descKey: "shortcuts.items.commandPalette" },
  { keys: ["Cmd", "B"], descKey: "shortcuts.items.sidebar" },
  { keys: ["Cmd", "N"], descKey: "shortcuts.items.newTask" },
  { keys: ["Cmd", "Shift", "A"], descKey: "shortcuts.items.approveAll" },
  { keys: ["Esc"], descKey: "shortcuts.items.closeDialog" },
  { keys: ["?"], descKey: "shortcuts.items.help" },
  { keys: ["g", "h"], descKey: "shortcuts.items.goHome" },
  { keys: ["g", "t"], descKey: "shortcuts.items.goTasks" },
  { keys: ["g", "a"], descKey: "shortcuts.items.goAgents" },
  { keys: ["g", "i"], descKey: "shortcuts.items.goInbox" },
  { keys: ["g", "s"], descKey: "shortcuts.items.goSettings" },
];

// ── Timezones ─────────────────────────────────────────────────────────────────

const TIMEZONES = [
  "Europe/Berlin",
  "Europe/Zurich",
  "Europe/Vienna",
  "Europe/London",
  "Europe/Paris",
  "Europe/Amsterdam",
  "Europe/Rome",
  "Europe/Madrid",
  "Europe/Stockholm",
  "Europe/Moscow",
  "America/New_York",
  "America/Chicago",
  "America/Denver",
  "America/Los_Angeles",
  "Asia/Tokyo",
  "Asia/Shanghai",
  "Asia/Kolkata",
  "Asia/Dubai",
  "Australia/Sydney",
  "Pacific/Auckland",
  "UTC",
];

// ── Autonomy Labels ───────────────────────────────────────────────────────────

// Action ids with catalog entries under settings.autonomy.actions.* — unknown
// backend actions fall back to their raw id instead of a missing-key path.
const KNOWN_AUTONOMY_ACTIONS = new Set([
  "deploy",
  "external_post",
  "config_change",
  "browser_action",
  "visual_review",
  "blocker_decision",
  "question",
  "code_change",
  "mark_done",
  "dispatch_escalation",
  "recovery_failed",
]);

const LEVEL_OPTIONS = [
  { value: "L1", labelKey: "autonomy.levels.L1", color: C.online },
  { value: "L2", labelKey: "autonomy.levels.L2", color: C.warning },
  { value: "L3", labelKey: "autonomy.levels.L3", color: C.error },
];

// ── Shared Components ─────────────────────────────────────────────────────────

function SectionHeader({ title, description }: { title: string; description: string }) {
  return (
    <div className="mb-6">
      <h2
        className="text-base font-semibold"
        style={{ color: "var(--color-text-primary)" }}
      >
        {title}
      </h2>
      <p
        className="text-sm mt-1"
        style={{ color: "var(--color-text-muted)" }}
      >
        {description}
      </p>
    </div>
  );
}

function FieldLabel({ children }: { children: React.ReactNode }) {
  return (
    <label
      className="text-xs font-medium uppercase tracking-widest block mb-1.5"
      style={{ color: "var(--color-text-secondary)" }}
    >
      {children}
    </label>
  );
}

const inputBaseClasses =
  "w-full rounded-lg px-3 py-2.5 text-sm outline-none transition-all duration-200";

const cardStyle = {
  background: C.bgSurface,
  border: `1px solid ${C.border}`,
  borderRadius: 12,
} as const;

function InputField({
  value,
  onChange,
  placeholder,
  type = "text",
  readOnly,
  rightElement,
  ariaLabel,
}: {
  value: string;
  onChange?: (v: string) => void;
  placeholder?: string;
  type?: string;
  readOnly?: boolean;
  rightElement?: React.ReactNode;
  ariaLabel?: string;
}) {
  return (
    <div className="relative">
      <input
        type={type}
        value={value}
        onChange={(e) => onChange?.(e.target.value)}
        placeholder={placeholder}
        aria-label={ariaLabel ?? placeholder}
        readOnly={readOnly}
        className={cn(
          inputBaseClasses,
          // No opacity on read-only: textMuted at 50% opacity put the email
          // address near 2:1. Read-only means "you cannot change this", not
          // "you cannot read this". The dimmed surface carries the state.
          readOnly ? "cursor-not-allowed" : "cursor-text",
          rightElement && "pr-10"
        )}
        style={{
          backgroundColor: readOnly ? "transparent" : C.bgDeep,
          borderWidth: 1,
          borderStyle: "solid",
          borderColor: readOnly ? "var(--color-border-subtle)" : "var(--color-border)",
          color: readOnly ? "var(--color-text-secondary)" : "var(--color-text-primary)",
        }}
        onFocus={(e) => {
          if (!readOnly) {
            e.currentTarget.style.borderColor = C.borderAccent;
          }
        }}
        onBlur={(e) => {
          e.currentTarget.style.borderColor = "var(--color-border)";
        }}
      />
      {rightElement && (
        <div className="absolute right-3 top-1/2 -translate-y-1/2">
          {rightElement}
        </div>
      )}
    </div>
  );
}

function SaveButton({
  onClick,
  loading,
  disabled,
  success,
  label,
}: {
  onClick: () => void;
  loading: boolean;
  disabled?: boolean;
  success?: boolean;
  label?: string;
}) {
  const t = useTranslations("settings");
  return (
    <button
      onClick={onClick}
      disabled={loading || disabled}
      className="flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium text-[var(--color-on-accent)] cursor-pointer transition-all duration-200 disabled:opacity-40 disabled:cursor-not-allowed"
      // Flat accent, not a gradient. A decorative 135deg ramp on the primary
      // action reads as a grey smear on this palette, and the system's first
      // rule is that nothing is coloured without meaning.
      style={{ background: success ? C.online : C.accent }}
    >
      {loading ? (
        <Loader2 size={14} className="animate-spin" />
      ) : success ? (
        <Check size={14} />
      ) : (
        <Save size={14} />
      )}
      {success ? t("saved") : label ?? t("save")}
    </button>
  );
}

function ErrorBanner({ message }: { message: string }) {
  return (
    <div
      className="flex items-center gap-2 text-xs rounded-lg px-3 py-2 mb-4"
      style={{
        backgroundColor: `${C.error}12`,
        border: `1px solid ${C.error}33`,
        color: C.error,
      }}
    >
      <AlertCircle size={14} />
      {message}
    </div>
  );
}

// ── Section transition wrapper ────────────────────────────────────────────────

function SectionMotion({ children, sectionKey }: { children: React.ReactNode; sectionKey: string }) {
  return (
    <motion.div
      key={sectionKey}
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: -4 }}
      transition={{ duration: 0.3, ease: [0.16, 1, 0.3, 1] }}
    >
      {children}
    </motion.div>
  );
}

// ── Profile Section ───────────────────────────────────────────────────────────

// UI language selector (i18n pattern page). Cookie-based: writes NEXT_LOCALE
// and refreshes the server tree — no URL change, applies immediately.
// Distinct from agent response language (per-agent `agents.language` field).
function LanguageField() {
  const t = useTranslations("settingsProfile");
  const locale = useLocale();
  const router = useRouter();

  function switchLocale(next: string) {
    document.cookie = `NEXT_LOCALE=${next}; path=/; max-age=31536000; samesite=lax`;
    router.refresh();
  }

  return (
    <div>
      <FieldLabel>{t("language")}</FieldLabel>
      <select
        value={locale}
        onChange={(e) => switchLocale(e.target.value)}
        aria-label={t("language")}
        className={inputBaseClasses}
        style={{
          backgroundColor: C.bgDeep,
          borderWidth: 1,
          borderStyle: "solid",
          borderColor: "var(--color-border)",
          color: "var(--color-text-primary)",
          cursor: "pointer",
        }}
      >
        <option value="en">{t("languageEn")}</option>
        <option value="de">{t("languageDe")}</option>
      </select>
      <p className="text-xs mt-1" style={{ color: "var(--color-text-muted)" }}>
        {t("languageHint")}
      </p>
    </div>
  );
}

function ProfileSection() {
  const t = useTranslations("settingsProfile");
  const { currentUser, setCurrentUser } = useAppStore();
  const [name, setName] = useState("");
  const [preferredName, setPreferredName] = useState("");
  const [timezone, setTimezone] = useState("Europe/Berlin");
  const [success, setSuccess] = useState(false);
  const [error, setError] = useState("");

  const { data: profile } = useQuery({
    queryKey: ["profile"],
    queryFn: api.auth.me,
  });

  useEffect(() => {
    if (profile) {
      setName(profile.name ?? "");
      setPreferredName(profile.preferred_name ?? "");
      setTimezone(profile.timezone ?? "Europe/Berlin");
    }
  }, [profile]);

  const mutation = useMutation({
    mutationFn: () =>
      api.auth.updateProfile({
        name: name.trim(),
        preferred_name: preferredName.trim(),
        timezone,
      }),
    onSuccess: (updated) => {
      setSuccess(true);
      setTimeout(() => setSuccess(false), 2000);
      setError("");
      if (currentUser) {
        const newUser: AuthUser = { ...currentUser, name: updated.name };
        setCurrentUser(newUser);
        setStoredUser(newUser);
      }
    },
    onError: (err: Error) => {
      setError(err.message.replace(/^.*?:\s*/, "").replace(/^"/, "").replace(/"$/, ""));
    },
  });

  const hasChanges =
    profile &&
    (name.trim() !== (profile.name ?? "") ||
      preferredName.trim() !== (profile.preferred_name ?? "") ||
      timezone !== (profile.timezone ?? "Europe/Berlin"));

  return (
    <SectionMotion sectionKey="profile">
      <SectionHeader title={t("title")} description={t("description")} />

      {error && <ErrorBanner message={error} />}

      <div className="mc-card p-6 space-y-5" style={cardStyle}>
        {/* Email (read-only) */}
        <div>
          <FieldLabel>{t("email")}</FieldLabel>
          <InputField value={profile?.email ?? ""} readOnly ariaLabel={t("email")} />
          <p className="text-xs mt-1" style={{ color: "var(--color-text-muted)" }}>
            {t("emailHint")}
          </p>
        </div>

        {/* Name */}
        <div>
          <FieldLabel>{t("name")}</FieldLabel>
          <InputField
            value={name}
            onChange={setName}
            placeholder={t("namePlaceholder")}
          />
        </div>

        {/* Preferred Name */}
        <div>
          <FieldLabel>{t("displayName")}</FieldLabel>
          <InputField
            value={preferredName}
            onChange={setPreferredName}
            placeholder={t("displayNamePlaceholder")}
          />
        </div>

        {/* UI language (i18n pattern) */}
        <LanguageField />

        {/* Timezone */}
        <div>
          <FieldLabel>{t("timezone")}</FieldLabel>
          <select
            value={timezone}
            onChange={(e) => setTimezone(e.target.value)}
            aria-label={t("timezoneAria")}
            className={inputBaseClasses}
            style={{
              backgroundColor: C.bgDeep,
              borderWidth: 1,
              borderStyle: "solid",
              borderColor: "var(--color-border)",
              color: "var(--color-text-primary)",
              cursor: "pointer",
            }}
            onFocus={(e) => {
              e.currentTarget.style.borderColor = C.borderAccent;
            }}
            onBlur={(e) => {
              e.currentTarget.style.borderColor = "var(--color-border)";
            }}
          >
            {TIMEZONES.map((tz) => (
              <option key={tz} value={tz}>
                {tz.replace(/_/g, " ")}
              </option>
            ))}
          </select>
        </div>

        {/* Role (read-only display) */}
        <div>
          <FieldLabel>{t("role")}</FieldLabel>
          <div className="flex items-center gap-2">
            <span
              className="px-2.5 py-1 rounded-md text-xs font-medium uppercase tracking-wider"
              style={{
                backgroundColor: C.accentSubtle,
                color: C.accent,
                border: `1px solid ${C.borderAccent}`,
              }}
            >
              {currentUser?.role ?? "viewer"}
            </span>
            <span className="text-xs" style={{ color: "var(--color-text-muted)" }}>
              {t("roleHint")}
            </span>
          </div>
        </div>

        {/* Save */}
        <div className="pt-2">
          <SaveButton
            onClick={() => mutation.mutate()}
            loading={mutation.isPending}
            disabled={!hasChanges}
            success={success}
          />
        </div>
      </div>
    </SectionMotion>
  );
}

// ── Security Section ──────────────────────────────────────────────────────────

function SecuritySection() {
  const t = useTranslations("settings.security");
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [showCurrent, setShowCurrent] = useState(false);
  const [showNew, setShowNew] = useState(false);
  const [success, setSuccess] = useState(false);
  const [error, setError] = useState("");

  const mutation = useMutation({
    mutationFn: () =>
      api.auth.updateProfile({
        current_password: currentPassword,
        new_password: newPassword,
      }),
    onSuccess: () => {
      setSuccess(true);
      setTimeout(() => setSuccess(false), 3000);
      setCurrentPassword("");
      setNewPassword("");
      setConfirmPassword("");
      setError("");
    },
    onError: (err: Error) => {
      setError(err.message.replace(/^.*?:\s*/, "").replace(/^"/, "").replace(/"$/, ""));
    },
  });

  function handleSubmit() {
    setError("");
    if (newPassword.length < 6) {
      setError(t("tooShort"));
      return;
    }
    if (newPassword !== confirmPassword) {
      setError(t("mismatch"));
      return;
    }
    mutation.mutate();
  }

  const canSubmit =
    currentPassword.length > 0 &&
    newPassword.length >= 6 &&
    confirmPassword.length > 0;

  return (
    <SectionMotion sectionKey="security">
      <SectionHeader title={t("title")} description={t("description")} />

      {error && <ErrorBanner message={error} />}

      <div className="mc-card p-6 space-y-5" style={cardStyle}>
        <h3
          className="text-sm font-medium"
          style={{ color: "var(--color-text-primary)" }}
        >
          {t("changePassword")}
        </h3>

        <div>
          <FieldLabel>{t("currentPassword")}</FieldLabel>
          <InputField
            type={showCurrent ? "text" : "password"}
            value={currentPassword}
            onChange={setCurrentPassword}
            placeholder={t("currentPasswordPlaceholder")}
            rightElement={
              <button
                type="button"
                onClick={() => setShowCurrent(!showCurrent)}
                className="cursor-pointer"
                style={{ color: "var(--color-text-muted)" }}
              >
                {showCurrent ? <EyeOff size={14} /> : <Eye size={14} />}
              </button>
            }
          />
        </div>

        <div>
          <FieldLabel>{t("newPassword")}</FieldLabel>
          <InputField
            type={showNew ? "text" : "password"}
            value={newPassword}
            onChange={setNewPassword}
            placeholder={t("newPasswordPlaceholder")}
            rightElement={
              <button
                type="button"
                onClick={() => setShowNew(!showNew)}
                className="cursor-pointer"
                style={{ color: "var(--color-text-muted)" }}
              >
                {showNew ? <EyeOff size={14} /> : <Eye size={14} />}
              </button>
            }
          />
        </div>

        <div>
          <FieldLabel>{t("confirmPassword")}</FieldLabel>
          <InputField
            type="password"
            value={confirmPassword}
            onChange={setConfirmPassword}
            placeholder={t("confirmPasswordPlaceholder")}
          />
          {confirmPassword && newPassword !== confirmPassword && (
            <p className="text-xs mt-1" style={{ color: C.error }}>
              {t("mismatch")}
            </p>
          )}
        </div>

        <SaveButton
          onClick={handleSubmit}
          loading={mutation.isPending}
          disabled={!canSubmit}
          success={success}
          label={t("changePassword")}
        />
      </div>
    </SectionMotion>
  );
}

// ── Autonomy Section (Admin only) ─────────────────────────────────────────────

function AutonomySection() {
  const t = useTranslations("settings");
  const qc = useQueryClient();

  const { data: config } = useQuery({
    queryKey: ["autonomy-config"],
    queryFn: api.settings.autonomy,
  });

  const updateMutation = useMutation({
    mutationFn: (levels: Record<string, string>) =>
      api.settings.updateAutonomy(levels),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["autonomy-config"] });
    },
  });

  const levels = config?.levels ?? {};
  const defaults = config?.defaults ?? {};

  const handleChange = (action: string, newLevel: string) => {
    updateMutation.mutate({ ...levels, [action]: newLevel });
  };

  return (
    <SectionMotion sectionKey="autonomy">
      <SectionHeader
        title={t("autonomy.title")}
        description={t("autonomy.description")}
      />

      <div className="mc-card p-4 sm:p-6" style={cardStyle}>
        {/* Desktop header row — hidden on mobile. The level columns are no
            longer labelled here: each row's buttons carry the words. */}
        <div
          className="hidden sm:grid items-center gap-3 px-3 pb-2"
          style={{
            gridTemplateColumns: "1fr 92px 92px 92px",
            color: "var(--color-text-muted)",
          }}
        >
          <span className="label-sys">{t("autonomy.action")}</span>
        </div>

        {/* Action Rows */}
        <div className="flex flex-col">
          {Object.keys(defaults).map((action) => {
            const meta = KNOWN_AUTONOMY_ACTIONS.has(action)
              ? {
                  label: t(`autonomy.actions.${action}.label`),
                  desc: t(`autonomy.actions.${action}.desc`),
                }
              : { label: action, desc: "" };
            const current = levels[action] ?? defaults[action] ?? "L3";
            const isDefault = !levels[action] || levels[action] === defaults[action];

            // One segmented control, shared by both layouts. The buttons used
            // to read "L1/L2/L3" while the column headers read
            // "Auto/Notify/Approve" — two vocabularies for one choice, plus a
            // legend underneath translating between them.
            const levelControl = (
              <div
                role="radiogroup"
                aria-label={meta.label}
                className="grid grid-cols-3 sm:contents rounded-md overflow-hidden"
                style={{ border: "1px solid var(--color-border)" }}
              >
                {LEVEL_OPTIONS.map((opt, i) => {
                  const isActive = current === opt.value;
                  return (
                    <button
                      key={opt.value}
                      role="radio"
                      aria-checked={isActive}
                      onClick={() => handleChange(action, opt.value)}
                      disabled={updateMutation.isPending}
                      title={`${opt.value} — ${t(opt.labelKey)}`}
                      className="flex items-center justify-center gap-1 h-8 sm:h-7 sm:rounded-md text-xs font-medium transition-colors cursor-pointer disabled:opacity-50 sm:border"
                      style={{
                        backgroundColor: isActive
                          ? `color-mix(in srgb, ${opt.color} 18%, transparent)`
                          : "transparent",
                        color: isActive ? opt.color : "var(--color-text-muted)",
                        borderLeft: i > 0 ? "1px solid var(--color-border)" : undefined,
                        borderColor: isActive ? opt.color : "var(--color-border)",
                      }}
                    >
                      {isActive && <Check size={11} className="shrink-0" />}
                      <span>{t(opt.labelKey)}</span>
                    </button>
                  );
                })}
              </div>
            );

            const name = (
              <div className="min-w-0">
                <div className="flex items-center gap-1.5 flex-wrap">
                  <span className="text-sm font-medium" style={{ color: "var(--color-text-primary)" }}>
                    {meta.label}
                  </span>
                  {!isDefault && (
                    <span
                      className="text-[10px] px-1 py-0.5 rounded"
                      style={{ color: C.accent, backgroundColor: C.accentSubtle }}
                    >
                      {t("autonomy.custom")}
                    </span>
                  )}
                </div>
                {meta.desc && (
                  <div className="text-xs mt-0.5" style={{ color: "var(--color-text-muted)" }}>
                    {meta.desc}
                  </div>
                )}
              </div>
            );

            return (
              // A plain row with a hairline, not a bordered card. Eleven cards
              // inside one card is a nested-card stack, and it cost ~92 px per
              // row for what is a 11x3 matrix.
              <div
                key={action}
                className="px-3 py-2.5 sm:grid sm:items-center sm:gap-3 transition-colors"
                style={{
                  gridTemplateColumns: "1fr 92px 92px 92px",
                  borderTop: "1px solid var(--color-border-subtle)",
                }}
              >
                <div className="mb-2 sm:mb-0">{name}</div>
                {levelControl}
              </div>
            );
          })}
        </div>
      </div>
    </SectionMotion>
  );
}

// ── Intelligence Section (Admin only) ─────────────────────────────────────────

function IntelligenceSection({
  onNavigateToAiProviders,
}: {
  onNavigateToAiProviders: () => void;
}) {
  const t = useTranslations("settings.intelligence");
  const queryClient = useQueryClient();
  const [config, setConfig] = useState<IntelligenceConfig | null>(null);
  // WHICH provider/model writes the report is decided on the AI-providers
  // page (single edit surface, ADR-055 pattern) — shown here read-only so
  // the operator sees the effective target without a second place to edit it.
  const { data: aiProviders } = useQuery<AiProviderSettingsResponse>({
    queryKey: ["ai-provider-settings"],
    queryFn: () => api.aiProviders.getSettings(),
  });
  const [success, setSuccess] = useState(false);
  const [triggerSuccess, setTriggerSuccess] = useState(false);
  const [error, setError] = useState("");

  const { data, isLoading } = useQuery({
    queryKey: ["intelligence-config"],
    queryFn: api.intelligence.config,
  });

  useEffect(() => {
    if (data) setConfig(data);
  }, [data]);

  const saveMutation = useMutation({
    mutationFn: (c: IntelligenceConfig) => api.intelligence.updateConfig(c),
    onSuccess: () => {
      setSuccess(true);
      setTimeout(() => setSuccess(false), 2000);
      setError("");
      queryClient.invalidateQueries({ queryKey: ["intelligence-config"] });
    },
    onError: (err: Error) => setError(err.message),
  });

  const triggerMutation = useMutation({
    mutationFn: () => api.intelligence.trigger(),
    onSuccess: () => {
      setTriggerSuccess(true);
      setTimeout(() => setTriggerSuccess(false), 3000);
      queryClient.invalidateQueries({ queryKey: ["intelligence-insights"] });
    },
    onError: (err: Error) => setError(err.message),
  });

  if (isLoading || !config) {
    return (
      <SectionMotion sectionKey="intelligence">
        <div className="flex items-center justify-center py-12">
          <Loader2 className="animate-spin" size={20} style={{ color: "var(--color-text-muted)" }} />
        </div>
      </SectionMotion>
    );
  }

  const update = (patch: Partial<IntelligenceConfig>) => setConfig({ ...config, ...patch });

  return (
    <SectionMotion sectionKey="intelligence">
      <SectionHeader title={t("title")} description={t("description")} />

      {error && <ErrorBanner message={error} />}

      <div className="space-y-6">
        {/* Enabled Toggle */}
        <div className="mc-card p-5 flex items-center justify-between" style={cardStyle}>
          <div>
            <span className="text-sm font-medium" style={{ color: "var(--color-text-primary)" }}>
              {t("serviceActive")}
            </span>
            <p className="text-xs mt-0.5" style={{ color: "var(--color-text-muted)" }}>
              {t("serviceActiveHint")}
            </p>
          </div>
          <button
            onClick={() => update({ enabled: !config.enabled })}
            className="relative w-11 h-6 rounded-full transition-colors cursor-pointer"
            style={{
              backgroundColor: config.enabled ? C.accent : "var(--color-bg-elevated)",
              border: config.enabled ? "none" : "1px solid var(--color-border)",
            }}
          >
            <span
              className="absolute top-0.5 w-5 h-5 rounded-full transition-transform"
              style={{
                left: config.enabled ? "calc(100% - 22px)" : "2px",
                // On the bone accent track a white knob vanishes (~1.1:1) —
                // dark knob on accent, light knob on the dark off-track.
                backgroundColor: config.enabled ? C.onAccent : "#fff",
              }}
            />
          </button>
        </div>

        {/* Analyse */}
        <div className="mc-card p-5 space-y-4" style={cardStyle}>
          <h3 className="text-sm font-medium" style={{ color: "var(--color-text-primary)" }}>
            {t("analysis")}
          </h3>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <div>
              <FieldLabel>{t("interval")}</FieldLabel>
              <InputField
                type="number"
                value={String(config.interval_seconds)}
                onChange={(v) => update({ interval_seconds: Math.max(60, parseInt(v) || 60) })}
              />
              <p className="text-xs mt-1" style={{ color: "var(--color-text-muted)" }}>{t("intervalHint")}</p>
            </div>
            <div>
              <FieldLabel>{t("window")}</FieldLabel>
              <InputField
                type="number"
                value={String(config.analysis_window_days)}
                onChange={(v) => update({ analysis_window_days: Math.max(1, parseInt(v) || 1) })}
              />
            </div>
          </div>
        </div>

        {/* Insights LLM — generation behaviour only. WHICH provider/model
            writes the report is configured on the AI-providers page; showing
            an editable model field here as well was two knobs on one engine
            (same dedup as the GitHub card in API Keys, ADR-055). */}
        <div className="mc-card p-5 space-y-4" style={cardStyle}>
          <h3 className="text-sm font-medium" style={{ color: "var(--color-text-primary)" }}>
            {t("insightsLlm")}
          </h3>
          <div
            className="flex items-center justify-between gap-4 rounded-lg px-3 py-2.5"
            style={{ backgroundColor: C.bgDeep, border: "1px solid var(--color-border)" }}
          >
            <div className="min-w-0">
              <span className="text-xs" style={{ color: "var(--color-text-muted)" }}>
                {t("insightsLlmManaged")}
              </span>
              <p
                data-testid="insights-llm-effective"
                className="text-sm font-mono truncate"
                style={{ color: "var(--color-text-primary)" }}
              >
                {aiProviders
                  ? `${aiProviders.values.ai_insights_provider ?? "spark"} · ${
                      aiProviders.insights_effective_model || t("insightsLlmModelAuto")
                    }`
                  : "…"}
              </p>
            </div>
            <button
              onClick={onNavigateToAiProviders}
              className="flex items-center gap-1.5 shrink-0 px-2.5 py-1.5 rounded-lg text-xs font-medium cursor-pointer transition-colors"
              style={{ backgroundColor: C.accentSubtle, color: C.accent }}
            >
              {t("goToAiProviders")}
              <ExternalLink size={12} />
            </button>
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <div>
              <FieldLabel>{t("temperature")}</FieldLabel>
              <InputField
                type="number"
                value={String(config.temperature)}
                onChange={(v) => {
                  const n = parseFloat(v);
                  if (!isNaN(n)) update({ temperature: Math.min(1, Math.max(0, n)) });
                }}
              />
              <p className="text-xs mt-1" style={{ color: "var(--color-text-muted)" }}>{t("temperatureHint")}</p>
            </div>
            <div>
              <FieldLabel>{t("maxTokens")}</FieldLabel>
              <InputField
                type="number"
                value={String(config.max_tokens)}
                onChange={(v) => update({ max_tokens: Math.min(8192, Math.max(100, parseInt(v) || 100)) })}
              />
              <p className="text-xs mt-1" style={{ color: "var(--color-text-muted)" }}>{t("maxTokensHint")}</p>
            </div>
          </div>
          <div>
            <FieldLabel>{t("systemPrompt")}</FieldLabel>
            <textarea
              aria-label={t("systemPromptAria")}
              value={config.system_prompt}
              onChange={(e) => update({ system_prompt: e.target.value })}
              rows={6}
              placeholder={t("systemPromptPlaceholder")}
              className={cn(inputBaseClasses, "resize-y")}
              style={{
                backgroundColor: C.bgDeep,
                borderWidth: 1,
                borderStyle: "solid",
                borderColor: "var(--color-border)",
                color: "var(--color-text-primary)",
              }}
              onFocus={(e) => {
                e.currentTarget.style.borderColor = C.borderAccent;
              }}
              onBlur={(e) => {
                e.currentTarget.style.borderColor = "var(--color-border)";
              }}
            />
            <p className="text-xs mt-1" style={{ color: "var(--color-text-muted)" }}>
              {t("systemPromptHint")}
            </p>
          </div>
        </div>

        {/* Schwellenwerte */}
        <div className="mc-card p-5 space-y-4" style={cardStyle}>
          <h3 className="text-sm font-medium" style={{ color: "var(--color-text-primary)" }}>
            {t("thresholds")}
          </h3>
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
            <div>
              <FieldLabel>{t("outlierMultiplier")}</FieldLabel>
              <InputField
                type="number"
                value={String(config.outlier_multiplier)}
                onChange={(v) => {
                  const n = parseFloat(v);
                  if (!isNaN(n) && n > 1) update({ outlier_multiplier: n });
                }}
              />
              <p className="text-xs mt-1" style={{ color: "var(--color-text-muted)" }}>{t("outlierHint")}</p>
            </div>
            <div>
              <FieldLabel>{t("successRateMin")}</FieldLabel>
              <InputField
                type="number"
                value={String(config.success_rate_threshold)}
                onChange={(v) => {
                  const n = parseFloat(v);
                  if (!isNaN(n)) update({ success_rate_threshold: Math.min(100, Math.max(0, n)) });
                }}
              />
            </div>
            <div>
              <FieldLabel>{t("failureMax")}</FieldLabel>
              <InputField
                type="number"
                value={String(config.failure_count_threshold)}
                onChange={(v) => update({ failure_count_threshold: Math.max(1, parseInt(v) || 1) })}
              />
            </div>
          </div>
        </div>

        {/* Actions */}
        <div className="flex items-center gap-3 pt-2">
          <SaveButton
            onClick={() => saveMutation.mutate(config)}
            loading={saveMutation.isPending}
            success={success}
          />
          <button
            onClick={() => triggerMutation.mutate()}
            disabled={triggerMutation.isPending}
            className="flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium transition-all duration-200 cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed"
            style={{
              backgroundColor: triggerSuccess ? C.online : "transparent",
              color: triggerSuccess ? "var(--color-on-accent)" : "var(--color-text-primary)",
              border: triggerSuccess ? "none" : "1px solid var(--color-border)",
            }}
          >
            {triggerMutation.isPending ? (
              <Loader2 size={14} className="animate-spin" />
            ) : triggerSuccess ? (
              <Check size={14} />
            ) : (
              <Play size={14} />
            )}
            {triggerSuccess ? t("analysisStarted") : t("analyzeNow")}
          </button>
        </div>
      </div>
    </SectionMotion>
  );
}

// ── API Keys Section (Admin only) ─────────────────────────────────────────────

function ApiKeysSection({
  onNavigateToGithub,
  onNavigateToSlack,
  onNavigateToTelegram,
}: {
  onNavigateToGithub: () => void;
  onNavigateToSlack: () => void;
  onNavigateToTelegram: () => void;
}) {
  const t = useTranslations("settings.apikeys");
  const tRoot = useTranslations("settings");
  const tProviderDesc = useTranslations("providerDescriptions");
  const queryClient = useQueryClient();
  const [addingKey, setAddingKey] = useState<string | null>(null);
  const [newValue, setNewValue] = useState("");
  const [editingKey, setEditingKey] = useState<string | null>(null);
  const [editValue, setEditValue] = useState("");
  const [showValue, setShowValue] = useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<ProviderTemplate | null>(null);

  const { data: providers } = useQuery<ProviderTemplate[]>({
    queryKey: ["secret-providers"],
    queryFn: () => api.secrets.providers(),
  });

  const { data: secrets, isLoading } = useQuery<SecretEntry[]>({
    queryKey: ["secrets"],
    queryFn: () => api.secrets.list(),
  });

  const createMutation = useMutation({
    mutationFn: (data: { key: string; value: string; provider?: string; label?: string; description?: string }) =>
      api.secrets.create(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["secrets"] });
      setAddingKey(null);
      setNewValue("");
    },
  });

  const updateMutation = useMutation({
    mutationFn: ({ key, value }: { key: string; value: string }) =>
      api.secrets.update(key, { value }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["secrets"] });
      setEditingKey(null);
      setEditValue("");
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (key: string) => api.secrets.delete(key),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["secrets"] });
    },
  });

  const secretsByKey = new Map(secrets?.map((s) => [s.key, s]) ?? []);

  // GitHub credentials (github_owner / github_token) have their own dedicated
  // "GitHub" section (ADR-055) — keep them out of the generic API Keys list
  // so there is exactly one place to edit them (ADR-055-Review MINOR 3).
  // Slack follows the same rule: both tokens belong to the Slack section,
  // which explains what each one is for and can test them.
  const nonGithubProviders = (providers ?? []).filter(
    (tmpl) =>
      tmpl.provider !== "github" && tmpl.provider !== "slack" && tmpl.provider !== "telegram"
  );
  const hasGithubSecret = (secrets ?? []).some(
    (s) => s.provider === "github" || s.key === "github_owner" || s.key === "github_token"
  );
  const hasSlackSecret = (secrets ?? []).some(
    (s) => s.provider === "slack" || s.key === "slack_bot_token" || s.key === "slack_app_token"
  );
  const hasTelegramSecret = (secrets ?? []).some(
    (s) =>
      s.provider === "telegram" ||
      s.key === "telegram_bot_token" ||
      s.key === "telegram_reports_bot_token"
  );

  return (
    <SectionMotion sectionKey="apikeys">
      <SectionHeader title={t("title")} description={t("description")} />

      {isLoading ? (
        <div className="flex items-center justify-center py-12">
          <Loader2 className="animate-spin" size={20} style={{ color: "var(--color-text-muted)" }} />
        </div>
      ) : (
        <div className="space-y-3">
          {hasGithubSecret && (
            <div className="mc-card p-4" style={cardStyle}>
              <div className="flex items-center justify-between gap-4">
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    <Github size={14} style={{ color: "var(--color-text-muted)" }} />
                    <span className="text-sm font-medium" style={{ color: "var(--color-text-primary)" }}>
                      GitHub
                    </span>
                  </div>
                  <p className="text-xs mt-0.5" style={{ color: "var(--color-text-muted)" }}>
                    {t("githubManagedHint")}
                  </p>
                </div>
                <button
                  onClick={onNavigateToGithub}
                  className="flex items-center gap-1.5 shrink-0 px-2.5 py-1.5 rounded-lg text-xs font-medium cursor-pointer transition-colors"
                  style={{ backgroundColor: C.accentSubtle, color: C.accent }}
                >
                  {t("goToGithub")}
                  <ExternalLink size={12} />
                </button>
              </div>
            </div>
          )}
          {hasSlackSecret && (
            <div className="mc-card p-4" style={cardStyle}>
              <div className="flex items-center justify-between gap-4">
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    <MessageSquare size={14} style={{ color: "var(--color-text-muted)" }} />
                    <span className="text-sm font-medium" style={{ color: "var(--color-text-primary)" }}>
                      Slack
                    </span>
                  </div>
                  <p className="text-xs mt-0.5" style={{ color: "var(--color-text-muted)" }}>
                    {t("slackManagedHint")}
                  </p>
                </div>
                <button
                  onClick={onNavigateToSlack}
                  className="flex items-center gap-1.5 shrink-0 px-2.5 py-1.5 rounded-lg text-xs font-medium cursor-pointer transition-colors"
                  style={{ backgroundColor: C.accentSubtle, color: C.accent }}
                >
                  {t("goToSlack")}
                  <ExternalLink size={12} />
                </button>
              </div>
            </div>
          )}
          {hasTelegramSecret && (
            <div className="mc-card p-4" style={cardStyle}>
              <div className="flex items-center justify-between gap-4">
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    <Send size={14} style={{ color: "var(--color-text-muted)" }} />
                    <span className="text-sm font-medium" style={{ color: "var(--color-text-primary)" }}>
                      Telegram
                    </span>
                  </div>
                  <p className="text-xs mt-0.5" style={{ color: "var(--color-text-muted)" }}>
                    {t("telegramManagedHint")}
                  </p>
                </div>
                <button
                  onClick={onNavigateToTelegram}
                  className="flex items-center gap-1.5 shrink-0 px-2.5 py-1.5 rounded-lg text-xs font-medium cursor-pointer transition-colors"
                  style={{ backgroundColor: C.accentSubtle, color: C.accent }}
                >
                  {t("goToTelegram")}
                  <ExternalLink size={12} />
                </button>
              </div>
            </div>
          )}
          {nonGithubProviders.map((tmpl) => {
            const existing = secretsByKey.get(tmpl.key);
            const isSet = !!existing;
            const isAdding = addingKey === tmpl.key;
            const isEditing = editingKey === tmpl.key;

            return (
              <div
                key={tmpl.key}
                className="mc-card p-4 transition-colors"
                style={cardStyle}
              >
                <div className="flex items-start justify-between gap-4">
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2">
                      <span
                        className="text-sm font-medium"
                        style={{ color: "var(--color-text-primary)" }}
                      >
                        {tmpl.label}
                      </span>
                      <span
                        className="text-[10px] px-1.5 py-0.5 rounded uppercase"
                        style={{
                          backgroundColor: isSet
                            ? `${C.online}1A`
                            : "var(--color-bg-elevated)",
                          color: isSet ? C.online : "var(--color-text-muted)",
                        }}
                      >
                        {isSet ? t("set") : t("notSet")}
                      </span>
                    </div>
                    <p className="text-xs mt-0.5" style={{ color: "var(--color-text-muted)" }}>
                      {tProviderDesc.has(tmpl.key) ? tProviderDesc(tmpl.key) : tmpl.description}
                    </p>
                    {existing && (
                      <div
                        className="text-xs font-mono mt-1.5"
                        style={{ color: "var(--color-text-secondary)" }}
                      >
                        {existing.value_masked}
                      </div>
                    )}
                  </div>

                  {/* Actions */}
                  <div className="flex items-center gap-1 shrink-0">
                    {existing ? (
                      <>
                        <button
                          onClick={() => {
                            setEditingKey(isEditing ? null : tmpl.key);
                            setEditValue("");
                          }}
                          className="px-2 py-1 rounded text-xs cursor-pointer transition-colors"
                          style={{ color: "var(--color-text-secondary)" }}
                        >
                          {isEditing ? t("cancel") : t("change")}
                        </button>
                        <button
                          onClick={() => setDeleteTarget(tmpl)}
                          className="px-2 py-1 rounded text-xs cursor-pointer transition-colors"
                          style={{ color: C.error }}
                        >
                          <Trash2 size={12} />
                        </button>
                      </>
                    ) : (
                      <button
                        onClick={() => {
                          setAddingKey(isAdding ? null : tmpl.key);
                          setNewValue("");
                        }}
                        className="flex items-center gap-1 px-2 py-1 rounded text-xs cursor-pointer transition-colors"
                        style={{
                          backgroundColor: isAdding
                            ? "var(--color-bg-elevated)"
                            : C.accentSubtle,
                          color: isAdding ? "var(--color-text-muted)" : C.accent,
                        }}
                      >
                        {isAdding ? <X size={12} /> : <Plus size={12} />}
                        {isAdding ? t("cancel") : t("add")}
                      </button>
                    )}
                  </div>
                </div>

                {/* Add form */}
                {isAdding && (
                  <div
                    className="mt-3 pt-3 border-t flex gap-2"
                    style={{ borderColor: "var(--color-border)" }}
                  >
                    <InputField
                      type={showValue === tmpl.key ? "text" : "password"}
                      value={newValue}
                      onChange={setNewValue}
                      placeholder={tmpl.placeholder}
                      rightElement={
                        <button
                          type="button"
                          onClick={() => setShowValue(showValue === tmpl.key ? null : tmpl.key)}
                          className="cursor-pointer"
                          style={{ color: "var(--color-text-muted)" }}
                        >
                          {showValue === tmpl.key ? <EyeOff size={14} /> : <Eye size={14} />}
                        </button>
                      }
                    />
                    <button
                      onClick={() => {
                        if (newValue) {
                          createMutation.mutate({
                            key: tmpl.key,
                            value: newValue,
                            provider: tmpl.provider,
                            label: tmpl.label,
                            description: tmpl.description,
                          });
                        }
                      }}
                      disabled={!newValue || createMutation.isPending}
                      className="shrink-0 px-3 py-2 rounded-lg text-xs font-medium cursor-pointer disabled:opacity-40 text-[var(--color-on-accent)]"
                      style={{
                        background: C.accent,
                      }}
                    >
                      {createMutation.isPending ? (
                        <Loader2 size={12} className="animate-spin" />
                      ) : (
                        tRoot("save")
                      )}
                    </button>
                  </div>
                )}

                {/* Edit form */}
                {isEditing && (
                  <div
                    className="mt-3 pt-3 border-t flex gap-2"
                    style={{ borderColor: "var(--color-border)" }}
                  >
                    <InputField
                      type={showValue === tmpl.key ? "text" : "password"}
                      value={editValue}
                      onChange={setEditValue}
                      placeholder={t("newValuePlaceholder")}
                      rightElement={
                        <button
                          type="button"
                          onClick={() => setShowValue(showValue === tmpl.key ? null : tmpl.key)}
                          className="cursor-pointer"
                          style={{ color: "var(--color-text-muted)" }}
                        >
                          {showValue === tmpl.key ? <EyeOff size={14} /> : <Eye size={14} />}
                        </button>
                      }
                    />
                    <button
                      onClick={() => {
                        if (editValue) {
                          updateMutation.mutate({ key: tmpl.key, value: editValue });
                        }
                      }}
                      disabled={!editValue || updateMutation.isPending}
                      className="shrink-0 px-3 py-2 rounded-lg text-xs font-medium cursor-pointer disabled:opacity-40 text-[var(--color-on-accent)]"
                      style={{
                        background: C.accent,
                      }}
                    >
                      {updateMutation.isPending ? (
                        <Loader2 size={12} className="animate-spin" />
                      ) : (
                        t("update")
                      )}
                    </button>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      {/* v3 confirm — replaces native confirm() (panel register rule 3) */}
      <ConfirmDialog
        open={!!deleteTarget}
        title={t("deleteTitle")}
        body={deleteTarget ? t("deleteBody", { label: deleteTarget.label }) : undefined}
        confirmLabel={t("delete")}
        loading={deleteMutation.isPending}
        onConfirm={() => {
          if (!deleteTarget) return;
          deleteMutation.mutate(deleteTarget.key, {
            onSuccess: () => setDeleteTarget(null),
          });
        }}
        onCancel={() => setDeleteTarget(null)}
      />
    </SectionMotion>
  );
}

// ── GitHub Section (ADR-055, admin only) ──────────────────────────────────────

function GithubSourceBadge({ source }: { source: "vault" | "env" | null }) {
  if (!source) return null;
  return (
    <span
      className="text-[10px] px-1.5 py-0.5 rounded uppercase"
      style={{ backgroundColor: "var(--color-bg-elevated)", color: "var(--color-text-muted)" }}
    >
      {source === "vault" ? "App" : ".env"}
    </span>
  );
}

function GithubSection() {
  const t = useTranslations("settings.github");
  const tRoot = useTranslations("settings");
  const queryClient = useQueryClient();
  const [owner, setOwner] = useState("");
  const [ownerTouched, setOwnerTouched] = useState(false);
  const [token, setToken] = useState("");
  const [probing, setProbing] = useState(false);
  const [probeResult, setProbeResult] = useState<GithubStatus | null>(null);
  const [saveMessage, setSaveMessage] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);

  const { data: status, isLoading } = useQuery<GithubStatus>({
    queryKey: ["github-status"],
    queryFn: () => api.repos.githubStatus(),
  });

  useEffect(() => {
    if (status && !ownerTouched) setOwner(status.owner ?? "");
  }, [status, ownerTouched]);

  const saveMutation = useMutation({
    mutationFn: (payload: GithubConfigUpdate) => api.repos.setGithubConfig(payload),
    onSuccess: async () => {
      setToken("");
      setOwnerTouched(false);
      setProbeResult(null);
      setSaveError(null);
      setSaveMessage(t("savedMsg"));
      await queryClient.invalidateQueries({ queryKey: ["github-status"] });
    },
    onError: (err) => {
      setSaveMessage(null);
      setSaveError(err instanceof Error ? err.message : t("saveFailed"));
    },
  });

  function handleSave() {
    setSaveMessage(null);
    setSaveError(null);
    const payload: GithubConfigUpdate = {};
    const trimmedOwner = owner.trim();
    if (ownerTouched) {
      // Empty only counts as an explicit delete if the field previously had a value.
      if (trimmedOwner === "" && status?.owner) payload.owner = "";
      else if (trimmedOwner !== "" && trimmedOwner !== (status?.owner ?? "")) payload.owner = trimmedOwner;
    }
    if (token.trim()) payload.token = token.trim();

    if (Object.keys(payload).length === 0) {
      setSaveMessage(t("nothingChanged"));
      return;
    }
    saveMutation.mutate(payload);
  }

  async function handleTest() {
    setProbing(true);
    setSaveMessage(null);
    setSaveError(null);
    try {
      const result = await api.repos.githubStatus(true);
      setProbeResult(result);
    } catch (err) {
      setProbeResult({
        owner: status?.owner ?? null,
        owner_source: status?.owner_source ?? null,
        token_set: status?.token_set ?? false,
        token_source: status?.token_source ?? null,
        configured: status?.configured ?? false,
        connected: false,
        login: null,
        owner_type: null,
        rate_limit_remaining: null,
        rate_limit_total: null,
        error: err instanceof Error ? err.message : t("testFailed"),
      });
    } finally {
      setProbing(false);
    }
  }

  const effective = probeResult ?? status ?? null;
  const connected = effective?.connected ?? null;
  const dotStatus: "online" | "error" | "idle" =
    connected === true ? "online" : connected === false ? "error" : "idle";
  const statusLabel =
    connected === true
      ? t("statusConnected")
      : connected === false
      ? t("statusFailed")
      : effective?.configured
      ? t("statusNotTested")
      : t("statusNotConnected");

  return (
    <SectionMotion sectionKey="github">
      <SectionHeader title={t("title")} description={t("description")} />

      <p className="text-sm mb-4" style={{ color: "var(--color-text-secondary)" }}>
        {t("introBefore")}{" "}
        <Link href="/repos" className="underline" style={{ color: C.accent }}>
          {t("introLink")}
        </Link>
        .
      </p>

      {isLoading ? (
        <div className="flex items-center justify-center py-12">
          <Loader2 className="animate-spin" size={20} style={{ color: "var(--color-text-muted)" }} />
        </div>
      ) : (
        <div className="space-y-4">
          {/* Status card */}
          <div className="mc-card p-4 space-y-2.5" style={cardStyle}>
            <div className="flex items-center gap-2">
              <StatusDot status={dotStatus} size="sm" />
              <span className="text-sm font-medium" style={{ color: "var(--color-text-primary)" }}>
                {statusLabel}
              </span>
              {probing && <Loader2 size={12} className="animate-spin" style={{ color: "var(--color-text-muted)" }} />}
            </div>

            <div className="flex items-center gap-2 text-xs">
              <span style={{ color: "var(--color-text-muted)" }}>{t("owner")}</span>
              <span className="font-mono" style={{ color: "var(--color-text-primary)" }}>
                {effective?.owner ?? "—"}
              </span>
              <GithubSourceBadge source={effective?.owner_source ?? null} />
            </div>

            <div className="flex items-center gap-2 text-xs">
              <span style={{ color: "var(--color-text-muted)" }}>{t("token")}</span>
              <span className="font-mono" style={{ color: "var(--color-text-primary)" }}>
                {effective?.token_set ? t("tokenSet") : t("tokenNotSet")}
              </span>
              <GithubSourceBadge source={effective?.token_source ?? null} />
            </div>

            {connected !== null && (
              <div className="pt-2 mt-1 space-y-1 text-xs" style={{ borderTop: `1px solid ${C.borderSubtle}`, color: "var(--color-text-muted)" }}>
                {effective?.login && (
                  <div>{t("authenticatedAs")} <span className="font-mono" style={{ color: "var(--color-text-secondary)" }}>{effective.login}</span></div>
                )}
                {effective?.owner_type && (
                  <div>{t("ownerType")} <span className="font-mono" style={{ color: "var(--color-text-secondary)" }}>{effective.owner_type}</span></div>
                )}
                {effective?.rate_limit_total != null && (
                  <div>
                    {t("rateLimit")}{" "}
                    <span className="font-mono" style={{ color: "var(--color-text-secondary)" }}>
                      {effective.rate_limit_remaining}/{effective.rate_limit_total}
                    </span>
                  </div>
                )}
              </div>
            )}

            {effective?.error && (
              <p className="text-xs pt-1" style={{ color: STATUS_TEXT.error }}>
                {effective.error}
              </p>
            )}

            <button
              onClick={handleTest}
              disabled={probing}
              className="mt-1 text-xs px-2.5 py-1.5 rounded-lg cursor-pointer disabled:opacity-50 transition-all"
              style={{ background: "var(--color-bg-elevated)", color: "var(--color-text-secondary)" }}
            >
              {probing ? t("testing") : t("testConnection")}
            </button>
          </div>

          {/* Form */}
          <div className="mc-card p-4 space-y-3" style={cardStyle}>
            <div>
              <FieldLabel>{t("owner")}</FieldLabel>
              <InputField
                value={owner}
                onChange={(v) => { setOwner(v); setOwnerTouched(true); }}
                placeholder={t("ownerPlaceholder")}
                ariaLabel={t("ownerAria")}
              />
            </div>
            <div>
              <FieldLabel>{t("token")}</FieldLabel>
              <InputField
                type="password"
                value={token}
                onChange={setToken}
                placeholder={status?.token_set ? t("tokenPlaceholderRotate") : "ghp_..."}
                ariaLabel={t("tokenAria")}
              />
            </div>

            {saveError && (
              <p className="text-xs rounded-lg px-3 py-2" style={{ color: STATUS_TEXT.error, backgroundColor: `${C.error}14`, border: `1px solid ${C.error}26` }}>
                {saveError}
              </p>
            )}
            {saveMessage && (
              <p className="text-xs rounded-lg px-3 py-2 flex items-center gap-1.5" style={{ color: C.online, backgroundColor: `${C.online}1A` }}>
                <Check size={12} /> {saveMessage}
              </p>
            )}

            <button
              onClick={handleSave}
              disabled={saveMutation.isPending}
              className="text-xs px-3 py-2 rounded-lg font-medium cursor-pointer disabled:opacity-40 text-[var(--color-on-accent)]"
              style={{ background: C.accent }}
            >
              {saveMutation.isPending ? <Loader2 size={12} className="animate-spin" /> : tRoot("save")}
            </button>
          </div>
        </div>
      )}
    </SectionMotion>
  );
}

// ── Users Section (Admin only) ────────────────────────────────────────────────

function UsersSection() {
  const t = useTranslations("settings.users");
  const queryClient = useQueryClient();
  const [showCreateForm, setShowCreateForm] = useState(false);

  const { data: users, isLoading } = useQuery({
    queryKey: ["admin-users"],
    queryFn: api.auth.users.list,
  });

  return (
    <SectionMotion sectionKey="users">
      <SectionHeader title={t("title")} description={t("description")} />

      {/* Create button */}
      <div className="flex justify-end mb-4">
        <button
          onClick={() => setShowCreateForm(!showCreateForm)}
          className="flex items-center gap-2 px-3 py-1.5 rounded-lg text-xs font-medium transition-all duration-200 cursor-pointer text-[var(--color-on-accent)]"
          style={{
            background: showCreateForm
              ? "transparent"
              : C.accent,
            color: showCreateForm ? "var(--color-text-secondary)" : "var(--color-on-accent)",
            border: showCreateForm ? "1px solid var(--color-border)" : "none",
          }}
        >
          {showCreateForm ? (
            <>
              <X size={12} /> {t("cancel")}
            </>
          ) : (
            <>
              <Plus size={12} /> {t("newUser")}
            </>
          )}
        </button>
      </div>

      {/* Create form */}
      {showCreateForm && (
        <CreateUserForm
          onCreated={() => {
            setShowCreateForm(false);
            queryClient.invalidateQueries({ queryKey: ["admin-users"] });
          }}
        />
      )}

      {/* Users list */}
      {isLoading ? (
        <div className="flex items-center justify-center py-12">
          <Loader2 className="animate-spin" size={20} style={{ color: "var(--color-text-muted)" }} />
        </div>
      ) : (
        <div className="space-y-2">
          {users?.map((user) => (
            <UserRow
              key={user.id}
              user={user}
              onUpdated={() =>
                queryClient.invalidateQueries({ queryKey: ["admin-users"] })
              }
            />
          ))}
        </div>
      )}
    </SectionMotion>
  );
}

// ── Create User Form ──────────────────────────────────────────────────────────

function CreateUserForm({ onCreated }: { onCreated: () => void }) {
  const t = useTranslations("settings.users");
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState("operator");
  const [error, setError] = useState("");

  const mutation = useMutation({
    mutationFn: () =>
      api.auth.users.create({ email: email.trim(), name: name.trim(), password, role }),
    onSuccess: () => {
      onCreated();
      setError("");
    },
    onError: (err: Error) => {
      setError(err.message.replace(/^.*?:\s*/, "").replace(/^"/, "").replace(/"$/, ""));
    },
  });

  return (
    <div className="mc-card p-5 mb-4 space-y-4" style={cardStyle}>
      {error && <ErrorBanner message={error} />}

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        <div>
          <FieldLabel>{t("name")}</FieldLabel>
          <InputField value={name} onChange={setName} placeholder={t("namePlaceholder")} />
        </div>
        <div>
          <FieldLabel>{t("email")}</FieldLabel>
          <InputField value={email} onChange={setEmail} placeholder={t("emailPlaceholder")} />
        </div>
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        <div>
          <FieldLabel>{t("password")}</FieldLabel>
          <InputField
            type="password"
            value={password}
            onChange={setPassword}
            placeholder={t("passwordPlaceholder")}
          />
        </div>
        <div>
          <FieldLabel>{t("role")}</FieldLabel>
          <select
            value={role}
            onChange={(e) => setRole(e.target.value)}
            aria-label={t("roleAria")}
            className={inputBaseClasses}
            style={{
              backgroundColor: C.bgDeep,
              borderWidth: 1,
              borderStyle: "solid",
              borderColor: "var(--color-border)",
              color: "var(--color-text-primary)",
              cursor: "pointer",
            }}
            onFocus={(e) => {
              e.currentTarget.style.borderColor = C.borderAccent;
            }}
            onBlur={(e) => {
              e.currentTarget.style.borderColor = "var(--color-border)";
            }}
          >
            <option value="admin">{t("roleAdmin")}</option>
            <option value="operator">{t("roleOperator")}</option>
            <option value="viewer">{t("roleViewer")}</option>
          </select>
        </div>
      </div>

      <SaveButton
        onClick={() => mutation.mutate()}
        loading={mutation.isPending}
        disabled={!email.trim() || !name.trim() || password.length < 6}
        label={t("createUser")}
      />
    </div>
  );
}

// ── User Row ──────────────────────────────────────────────────────────────────

function UserRow({
  user,
  onUpdated,
}: {
  user: AuthUser & { is_active: boolean; has_password: boolean; created_at: string };
  onUpdated: () => void;
}) {
  const t = useTranslations("settings.users");
  const currentUser = useAppStore((s) => s.currentUser);
  const isSelf = currentUser?.id === user.id;
  const [editing, setEditing] = useState(false);
  const [role, setRole] = useState(user.role);
  const [error, setError] = useState("");

  const updateMutation = useMutation({
    mutationFn: (data: { role?: string; is_active?: boolean }) =>
      api.auth.users.update(user.id, data),
    onSuccess: () => {
      onUpdated();
      setEditing(false);
      setError("");
    },
    onError: (err: Error) => {
      setError(err.message.replace(/^.*?:\s*/, "").replace(/^"/, "").replace(/"$/, ""));
    },
  });

  const roleColors: Record<string, { bg: string; text: string }> = {
    admin: { bg: C.accentSubtle, text: C.accent },
    operator: { bg: `${C.warning}1F`, text: C.warning },
    viewer: { bg: "var(--color-bg-elevated)", text: "var(--color-text-muted)" },
  };

  const rc = roleColors[user.role] ?? roleColors.viewer;

  return (
    <div
      className="mc-card px-4 py-3 transition-colors"
      style={{ ...cardStyle, opacity: user.is_active ? 1 : 0.5 }}
    >
      {/* Top row: avatar + info + role */}
      <div className="flex items-center gap-3">
        {/* Avatar circle */}
        <div
          className="w-9 h-9 rounded-full flex items-center justify-center shrink-0 text-sm font-semibold"
          style={{
            backgroundColor: "var(--color-bg-elevated)",
            color: "var(--color-text-secondary)",
          }}
        >
          {(user.name ?? "?").charAt(0).toUpperCase()}
        </div>

        {/* Info */}
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-1.5 flex-wrap">
            <span
              className="text-sm font-medium"
              style={{ color: "var(--color-text-primary)" }}
            >
              {user.name}
            </span>
            {isSelf && (
              <span
                className="text-[10px] px-1.5 py-0.5 rounded"
                style={{
                  backgroundColor: "var(--color-bg-elevated)",
                  color: "var(--color-text-muted)",
                }}
              >
                {t("you")}
              </span>
            )}
            {!user.is_active && (
              <span
                className="text-[10px] px-1.5 py-0.5 rounded"
                style={{
                  backgroundColor: `${C.error}1F`,
                  color: C.error,
                }}
              >
                {t("deactivated")}
              </span>
            )}
            {/* Role badge — inline with name */}
            {!editing ? (
              <span
                className="px-1.5 py-0.5 rounded text-[10px] font-medium uppercase"
                style={{ backgroundColor: rc.bg, color: rc.text }}
              >
                {user.role}
              </span>
            ) : (
              <select
                aria-label={t("roleChangeAria")}
                value={role}
                onChange={(e) => setRole(e.target.value)}
                className="rounded px-2 py-1 text-xs outline-none cursor-pointer"
                style={{
                  backgroundColor: C.bgDeep,
                  border: "1px solid var(--color-border)",
                  color: "var(--color-text-primary)",
                }}
              >
                <option value="admin">{t("roleAdmin")}</option>
                <option value="operator">{t("roleOperator")}</option>
                <option value="viewer">{t("roleViewer")}</option>
              </select>
            )}
          </div>
          <div className="text-xs truncate" style={{ color: "var(--color-text-muted)" }}>
            {user.email}
          </div>
        </div>
      </div>

      {/* Actions row — below on all sizes */}
      {!isSelf && (
        <div className="flex items-center gap-1 mt-2 pl-12">
          {editing ? (
            <>
              <button
                onClick={() => updateMutation.mutate({ role })}
                disabled={updateMutation.isPending}
                className="px-2 py-1 rounded text-xs font-medium cursor-pointer transition-colors text-[var(--color-on-accent)]"
                style={{ background: C.accent }}
              >
                {updateMutation.isPending ? (
                  <Loader2 size={12} className="animate-spin" />
                ) : (
                  t("save")
                )}
              </button>
              <button
                onClick={() => {
                  setEditing(false);
                  setRole(user.role);
                  setError("");
                }}
                className="px-2 py-1 rounded text-xs cursor-pointer"
                style={{ color: "var(--color-text-muted)" }}
              >
                {t("cancel")}
              </button>
            </>
          ) : (
            <>
              <button
                onClick={() => setEditing(true)}
                className="px-2 py-1 rounded text-xs cursor-pointer transition-colors"
                style={{ color: "var(--color-text-secondary)" }}
              >
                {t("edit")}
              </button>
              <button
                onClick={() =>
                  updateMutation.mutate({ is_active: !user.is_active })
                }
                className="px-2 py-1 rounded text-xs cursor-pointer transition-colors"
                style={{
                  color: user.is_active ? C.error : C.online,
                }}
              >
                {user.is_active ? t("deactivate") : t("activate")}
              </button>
            </>
          )}
        </div>
      )}

      {/* Error */}
      {error && (
        <div className="text-xs" style={{ color: C.error }}>
          {error}
        </div>
      )}
    </div>
  );
}

// ── Shortcuts Section ─────────────────────────────────────────────────────────

function ShortcutsSection() {
  const t = useTranslations("settings");
  const stagger = {
    initial: { opacity: 0, y: 8 },
    animate: (i: number) => ({
      opacity: 1,
      y: 0,
      transition: { delay: i * 0.05, duration: 0.3, ease: [0.16, 1, 0.3, 1] },
    }),
  };

  return (
    <SectionMotion sectionKey="shortcuts">
      <SectionHeader
        title={t("shortcuts.title")}
        description={t("shortcuts.description")}
      />

      <div className="mc-card p-6" style={cardStyle}>
        <div className="space-y-1">
          {SHORTCUTS.map((shortcut, i) => (
            <motion.div
              key={shortcut.descKey}
              custom={i}
              initial="initial"
              animate="animate"
              variants={stagger}
              className="flex items-center justify-between py-2.5 px-3 rounded-lg transition-colors"
              style={{
                backgroundColor:
                  i % 2 === 0 ? "var(--color-bg-surface)" : "transparent",
              }}
            >
              <span className="text-sm" style={{ color: "var(--color-text-body)" }}>
                {t(shortcut.descKey)}
              </span>
              <div className="flex items-center gap-1">
                {shortcut.keys.map((key, j) => (
                  <span key={j}>
                    {j > 0 && (
                      <span
                        className="text-xs mx-0.5"
                        style={{ color: "var(--color-text-muted)" }}
                      >
                        +
                      </span>
                    )}
                    <kbd
                      className="inline-block px-2 py-1 rounded text-xs font-mono"
                      style={{
                        backgroundColor: "var(--color-bg-elevated)",
                        border: "1px solid var(--color-border)",
                        color: "var(--color-text-secondary)",
                        boxShadow: "0 1px 2px rgba(0, 0, 0, 0.3)",
                      }}
                    >
                      {key}
                    </kbd>
                  </span>
                ))}
              </div>
            </motion.div>
          ))}
        </div>
      </div>
    </SectionMotion>
  );
}

// ── About Section ─────────────────────────────────────────────────────────────

function AboutSection() {
  const t = useTranslations("settings.about");
  const { data: version } = useQuery({
    queryKey: ["system-version"],
    queryFn: api.system.version,
    staleTime: 60 * 60 * 1000,
  });
  return (
    <SectionMotion sectionKey="about">
      <SectionHeader title={t("title")} description={t("description")} />

      <div className="space-y-6">
        {/* System info */}
        <div className="mc-card p-6" style={cardStyle}>
          <h3
            className="text-sm font-semibold mb-6"
            style={{ color: "var(--color-text-primary)" }}
          >
            {t("system")}
          </h3>
          <div className="space-y-4">
            {[
              { label: t("version"), value: version?.current ?? "…" },
              { label: t("frontend"), value: "Next.js 15 + TypeScript + Tailwind v4" },
              { label: t("backend"), value: "FastAPI + PostgreSQL + Redis" },
            ].map(({ label, value }) => (
              <div key={label} className="flex items-center justify-between">
                <span className="text-sm" style={{ color: "var(--color-text-secondary)" }}>
                  {label}
                </span>
                <span className="text-sm font-mono" style={{ color: "var(--color-text-primary)" }}>
                  {value}
                </span>
              </div>
            ))}
            {version?.update_available && version.release_url && (
              <div className="flex items-center justify-between pt-2" style={{ borderTop: "1px solid var(--color-border)" }}>
                <span className="text-sm" style={{ color: "var(--color-warning)" }}>
                  {t("updateAvailable", { version: version.latest ?? "" })}
                </span>
                <a
                  href={version.release_url}
                  target="_blank"
                  rel="noreferrer"
                  className="text-sm font-mono hover:underline"
                  style={{ color: "var(--color-accent)" }}
                >
                  {t("releaseNotes")}
                </a>
              </div>
            )}
          </div>
        </div>

        {/* Links */}
        <div className="mc-card p-6" style={cardStyle}>
          <h3
            className="text-sm font-semibold mb-4"
            style={{ color: "var(--color-text-primary)" }}
          >
            {t("links")}
          </h3>
          <div className="space-y-2">
            <a
              href={`https://github.com/${process.env.NEXT_PUBLIC_GITHUB_OWNER || "your-github-user"}`}
              target="_blank"
              rel="noopener noreferrer"
              className="flex items-center gap-2 text-sm transition-colors"
              style={{ color: "var(--color-text-secondary)" }}
              onMouseEnter={(e) =>
                (e.currentTarget.style.color = C.accent)
              }
              onMouseLeave={(e) =>
                (e.currentTarget.style.color = "var(--color-text-secondary)")
              }
            >
              <ExternalLink size={13} />
              GitHub
            </a>
          </div>
        </div>

        {/* Credits */}
        <div
          className="text-center py-4 text-xs"
          style={{ color: "var(--color-text-muted)" }}
        >
          {t("credits")}
        </div>
      </div>
    </SectionMotion>
  );
}

// ── Main Page ─────────────────────────────────────────────────────────────────

function SettingsContent() {
  const t = useTranslations("settings");
  // Deep-link support: /settings?section=github lets other pages link
  // straight into a section (e.g. the /repos onboarding banner).
  const searchParams = useSearchParams();
  const sectionParam = searchParams.get("section");
  const [activeSection, setActiveSection] = useState(
    sectionParam && SECTIONS.some((s) => s.id === sectionParam) ? sectionParam : "profile"
  );
  useEffect(() => {
    if (sectionParam && SECTIONS.some((s) => s.id === sectionParam)) {
      setActiveSection(sectionParam);
    }
  }, [sectionParam]);

  const currentUser = useAppStore((s) => s.currentUser);
  const isAdmin = currentUser?.role === "admin";

  const visibleSections = SECTIONS.filter((s) => !s.adminOnly || isAdmin);

  return (
    <div className="h-full flex flex-col overflow-hidden md:-m-6">
      {/* Header */}
      <motion.div
        initial={{ opacity: 0, y: 12 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.4, ease: [0.16, 1, 0.3, 1] }}
        className="shrink-0 px-4 py-4 md:px-6"
      >
        {/* label-sys stays untranslated: P2 mono instrument code (round-6 decision) */}
        <div className="label-sys mb-2">System · Settings</div>
        <h1 className="display text-2xl font-semibold" style={{ color: "var(--color-text-primary)" }}>{t("title")}</h1>
        <p className="text-[13px] mt-1" style={{ color: "var(--color-text-secondary)" }}>
          {t("subtitle")}
        </p>
        {/* Messmarke: 1px-Linie mit Akzent-Segment — Header-Trenner */}
        <div className="relative mt-4 h-px" style={{ backgroundColor: C.border }}>
          <div
            className="absolute left-0 -top-px h-[2px] w-16"
            style={{ backgroundColor: C.accent }}
          />
        </div>
      </motion.div>

      <div className="flex-1 flex flex-col md:flex-row overflow-hidden">
        {/* Left: Section Nav (glass sidebar) */}
        <motion.nav
          initial={{ opacity: 0, x: -8 }}
          animate={{ opacity: 1, x: 0 }}
          transition={{ delay: 0.1, duration: 0.3, ease: [0.16, 1, 0.3, 1] }}
          className="w-full md:w-52 shrink-0 border-b md:border-b-0 md:border-r overflow-x-auto md:overflow-y-auto py-3 tab-strip-nav"
          style={{
            borderColor: "var(--color-border-subtle)",
            // Transparent on desktop: the filled panel used to stop mid-page
            // with a hard edge floating in the dark. The divider carries the
            // separation; the surface is only needed for the mobile strip.
            backgroundColor: "transparent",
          }}
        >
          {GROUP_ORDER.map((group) => {
            const items = visibleSections.filter((s) => s.group === group);
            if (items.length === 0) return null;
            return (
              <div key={group} className="md:mb-3 last:mb-0 flex md:block items-center">
                {/* Group labels are desktop-only: on mobile the nav is one
                    horizontal strip, where headings would break the scan. */}
                <div className="hidden md:block label-sys px-3 pb-1 pt-2">
                  {t(`groups.${group}`)}
                </div>
                <ul className="flex md:flex-col gap-1 md:gap-0.5 px-2 min-w-max md:min-w-0">
            {items.map((section) => {
              const Icon = section.icon;
              const isActive = activeSection === section.id;
              return (
                <li key={section.id}>
                  <button
                    onClick={() => setActiveSection(section.id)}
                    className={cn(
                      "relative flex items-center gap-3 w-full px-3 py-2 rounded-lg text-sm transition-all duration-200 cursor-pointer",
                      isActive ? "font-medium" : ""
                    )}
                    style={{
                      color: isActive
                        ? "var(--color-text-primary)"
                        : "var(--color-text-secondary)",
                      backgroundColor: isActive
                        ? C.accentSubtle
                        : "transparent",
                    }}
                    onMouseEnter={(e) => {
                      if (!isActive) {
                        e.currentTarget.style.backgroundColor = "var(--color-bg-surface)";
                      }
                    }}
                    onMouseLeave={(e) => {
                      if (!isActive) {
                        e.currentTarget.style.backgroundColor = "transparent";
                      }
                    }}
                  >
                    {/* Active state = accent-subtle surface + accent icon (DESIGN.md
                        navigation pattern) — no side-stripe indicator on top. */}
                    <Icon
                      size={16}
                      style={{
                        color: isActive ? C.accent : undefined,
                      }}
                    />
                    <span className="whitespace-nowrap">{t(section.labelKey)}</span>
                  </button>
                </li>
              );
            })}
                </ul>
              </div>
            );
          })}
        </motion.nav>

        {/* Right: Section Content */}
        <div className="flex-1 overflow-y-auto p-4 md:p-6 min-w-0">
          {/* 3xl, not 2xl: two nav columns already eat ~530 px of a 1440 px
              screen, and the dense sections (autonomy matrix, key lists) were
              being squeezed while 300 px sat empty on the right. */}
          <div className="max-w-3xl min-w-0">
            <AnimatePresence mode="wait">
              {activeSection === "profile" && <ProfileSection />}
              {activeSection === "security" && <SecuritySection />}
              {activeSection === "autonomy" && isAdmin && <AutonomySection />}
              {activeSection === "intelligence" && isAdmin && (
                <IntelligenceSection
                  onNavigateToAiProviders={() => setActiveSection("ai-providers")}
                />
              )}
              {activeSection === "apikeys" && isAdmin && (
                <ApiKeysSection
                  onNavigateToGithub={() => setActiveSection("github")}
                  onNavigateToSlack={() => setActiveSection("slack")}
                  onNavigateToTelegram={() => setActiveSection("telegram")}
                />
              )}
              {activeSection === "github" && isAdmin && <GithubSection />}
              {activeSection === "slack" && isAdmin && <SlackTab />}
              {activeSection === "telegram" && isAdmin && <TelegramTab />}
              {activeSection === "ai-providers" && isAdmin && <AiProvidersTab />}
              {activeSection === "credentials" && isAdmin && <CredentialsTab />}
              {activeSection === "costs" && isAdmin && <CostPricesTab />}
              {activeSection === "users" && isAdmin && <UsersSection />}
              {activeSection === "shortcuts" && <ShortcutsSection />}
              {activeSection === "about" && <AboutSection />}
            </AnimatePresence>
          </div>
        </div>
      </div>
    </div>
  );
}

export default function SettingsPage() {
  return (
    <AppShell>
      <Suspense fallback={null}>
        <SettingsContent />
      </Suspense>
    </AppShell>
  );
}
