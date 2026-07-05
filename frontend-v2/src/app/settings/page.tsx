"use client";

import { Suspense, useState, useEffect } from "react";
import { useSearchParams } from "next/navigation";
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
  type LucideIcon,
} from "lucide-react";
import { api, setStoredUser } from "@/lib/api";
import { useAppStore, type AuthUser } from "@/lib/store";
import type {
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
import { StatusDot } from "@/components/shared/StatusDot";
import { C, STATUS_TEXT } from "@/lib/colors";

// ── Section Registry ──────────────────────────────────────────────────────────

interface SettingsSection {
  id: string;
  label: string;
  icon: LucideIcon;
  adminOnly?: boolean;
}

const SECTIONS: SettingsSection[] = [
  { id: "profile", label: "Profile", icon: User },
  { id: "security", label: "Security", icon: Shield },
  { id: "autonomy", label: "Autonomy", icon: SlidersHorizontal, adminOnly: true },
  { id: "intelligence", label: "Intelligence", icon: Zap, adminOnly: true },
  { id: "apikeys", label: "API Keys", icon: Key, adminOnly: true },
  { id: "github", label: "GitHub", icon: Github, adminOnly: true },
  { id: "credentials", label: "Credentials", icon: KeyRound, adminOnly: true },
  { id: "costs", label: "Costs", icon: DollarSign, adminOnly: true },
  { id: "users", label: "Users", icon: Users, adminOnly: true },
  { id: "shortcuts", label: "Shortcuts", icon: Keyboard },
  { id: "about", label: "About", icon: Info },
];

// ── Keyboard shortcuts reference ──────────────────────────────────────────────

const SHORTCUTS = [
  { keys: ["Cmd", "K"], description: "Open command palette" },
  { keys: ["Cmd", "B"], description: "Collapse/expand sidebar" },
  { keys: ["Cmd", "N"], description: "New task" },
  { keys: ["Cmd", "Shift", "A"], description: "Approve all approvals" },
  { keys: ["Esc"], description: "Close dialog/palette" },
  { keys: ["?"], description: "Help (command palette)" },
  { keys: ["g", "h"], description: "Go to Home" },
  { keys: ["g", "t"], description: "Go to Tasks" },
  { keys: ["g", "a"], description: "Go to Agents" },
  { keys: ["g", "i"], description: "Go to Inbox" },
  { keys: ["g", "s"], description: "Go to Settings" },
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

const AUTONOMY_LABELS: Record<string, { label: string; desc: string }> = {
  deploy: { label: "Deploy", desc: "Vercel/Cloudflare deployments" },
  external_post: { label: "External Post", desc: "Social media, emails" },
  config_change: { label: "Config Change", desc: "Change system configuration" },
  browser_action: { label: "Browser Action", desc: "Visit websites" },
  visual_review: { label: "Visual Review", desc: "Screenshot comparisons" },
  blocker_decision: { label: "Blocker Decision", desc: "Escalate blocked tasks" },
  question: { label: "Question", desc: "Questions to the operator" },
  code_change: { label: "Code Change", desc: "Write/change code" },
  mark_done: { label: "Mark Done", desc: "Mark tasks as done" },
  dispatch_escalation: { label: "Dispatch Escalation", desc: "Agent not responding" },
  recovery_failed: { label: "Recovery Failed", desc: "Automatic recovery failed" },
};

const LEVEL_OPTIONS = [
  { value: "L1", label: "Auto", color: C.online },
  { value: "L2", label: "Notify", color: C.warning },
  { value: "L3", label: "Approve", color: C.error },
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
          readOnly ? "opacity-50 cursor-not-allowed" : "cursor-text",
          rightElement && "pr-10"
        )}
        style={{
          backgroundColor: C.bgDeep,
          borderWidth: 1,
          borderStyle: "solid",
          borderColor: "rgba(255, 255, 255, 0.08)",
          color: readOnly ? "var(--color-text-muted)" : "var(--color-text-primary)",
        }}
        onFocus={(e) => {
          if (!readOnly) {
            e.currentTarget.style.borderColor = C.borderAccent;
          }
        }}
        onBlur={(e) => {
          e.currentTarget.style.borderColor = "rgba(255, 255, 255, 0.08)";
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
  label = "Save",
}: {
  onClick: () => void;
  loading: boolean;
  disabled?: boolean;
  success?: boolean;
  label?: string;
}) {
  return (
    <button
      onClick={onClick}
      disabled={loading || disabled}
      className="flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium text-white cursor-pointer transition-all duration-200 disabled:opacity-40 disabled:cursor-not-allowed"
      style={{
        background: success
          ? C.online
          : `linear-gradient(135deg, ${C.accent}, ${C.accentHover})`,
      }}
    >
      {loading ? (
        <Loader2 size={14} className="animate-spin" />
      ) : success ? (
        <Check size={14} />
      ) : (
        <Save size={14} />
      )}
      {success ? "Saved" : label}
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

function ProfileSection() {
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
      <SectionHeader title="Profile" description="Your personal information." />

      {error && <ErrorBanner message={error} />}

      <div className="mc-card p-6 space-y-5" style={cardStyle}>
        {/* Email (read-only) */}
        <div>
          <FieldLabel>Email</FieldLabel>
          <InputField value={profile?.email ?? ""} readOnly ariaLabel="Email (read-only)" />
          <p className="text-xs mt-1" style={{ color: "var(--color-text-muted)" }}>
            Email cannot be changed.
          </p>
        </div>

        {/* Name */}
        <div>
          <FieldLabel>Name</FieldLabel>
          <InputField
            value={name}
            onChange={setName}
            placeholder="Your full name"
          />
        </div>

        {/* Preferred Name */}
        <div>
          <FieldLabel>Display Name</FieldLabel>
          <InputField
            value={preferredName}
            onChange={setPreferredName}
            placeholder="Optional: how you'd like to be addressed"
          />
        </div>

        {/* Timezone */}
        <div>
          <FieldLabel>Timezone</FieldLabel>
          <select
            value={timezone}
            onChange={(e) => setTimezone(e.target.value)}
            aria-label="Select timezone"
            className={inputBaseClasses}
            style={{
              backgroundColor: C.bgDeep,
              borderWidth: 1,
              borderStyle: "solid",
              borderColor: "rgba(255, 255, 255, 0.08)",
              color: "var(--color-text-primary)",
              cursor: "pointer",
            }}
            onFocus={(e) => {
              e.currentTarget.style.borderColor = C.borderAccent;
            }}
            onBlur={(e) => {
              e.currentTarget.style.borderColor = "rgba(255, 255, 255, 0.08)";
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
          <FieldLabel>Role</FieldLabel>
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
              Can only be changed by an admin.
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
      setError("New password must be at least 6 characters long.");
      return;
    }
    if (newPassword !== confirmPassword) {
      setError("Passwords do not match.");
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
      <SectionHeader
        title="Security"
        description="Change your password and manage security settings."
      />

      {error && <ErrorBanner message={error} />}

      <div className="mc-card p-6 space-y-5" style={cardStyle}>
        <h3
          className="text-sm font-medium"
          style={{ color: "var(--color-text-primary)" }}
        >
          Change Password
        </h3>

        <div>
          <FieldLabel>Current Password</FieldLabel>
          <InputField
            type={showCurrent ? "text" : "password"}
            value={currentPassword}
            onChange={setCurrentPassword}
            placeholder="Your current password"
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
          <FieldLabel>New Password</FieldLabel>
          <InputField
            type={showNew ? "text" : "password"}
            value={newPassword}
            onChange={setNewPassword}
            placeholder="Min. 6 characters"
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
          <FieldLabel>Confirm New Password</FieldLabel>
          <InputField
            type="password"
            value={confirmPassword}
            onChange={setConfirmPassword}
            placeholder="Enter again"
          />
          {confirmPassword && newPassword !== confirmPassword && (
            <p className="text-xs mt-1" style={{ color: C.error }}>
              Passwords do not match.
            </p>
          )}
        </div>

        <SaveButton
          onClick={handleSubmit}
          loading={mutation.isPending}
          disabled={!canSubmit}
          success={success}
          label="Change Password"
        />
      </div>
    </SectionMotion>
  );
}

// ── Autonomy Section (Admin only) ─────────────────────────────────────────────

function AutonomySection() {
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
        title="Autonomy Levels"
        description="Decide for each action whether agents act autonomously (L1), notify you (L2), or wait for approval (L3)."
      />

      <div className="mc-card p-4 sm:p-6" style={cardStyle}>
        {/* Desktop header row — hidden on mobile */}
        <div
          className="hidden sm:grid items-center gap-3 px-3 py-2 mb-1"
          style={{
            gridTemplateColumns: "1fr 80px 80px 80px",
            color: "var(--color-text-muted)",
          }}
        >
          <span className="text-xs font-medium uppercase tracking-wide">Action</span>
          {LEVEL_OPTIONS.map((opt) => (
            <span key={opt.value} className="text-xs font-medium text-center" style={{ color: opt.color }}>
              {opt.label}
            </span>
          ))}
        </div>

        {/* Action Rows */}
        <div className="flex flex-col gap-1.5">
          {Object.keys(defaults).map((action) => {
            const meta = AUTONOMY_LABELS[action] ?? { label: action, desc: "" };
            const current = levels[action] ?? defaults[action] ?? "L3";
            const isDefault = !levels[action] || levels[action] === defaults[action];

            return (
              <div
                key={action}
                className="rounded-md px-3 py-2.5 transition-colors"
                style={{
                  backgroundColor: "rgba(255, 255, 255, 0.02)",
                  border: "1px solid rgba(255, 255, 255, 0.04)",
                }}
              >
                {/* Mobile: stacked layout */}
                <div className="sm:hidden">
                  <div className="flex items-center gap-1.5 mb-2">
                    <span className="text-sm font-medium" style={{ color: "var(--color-text-primary)" }}>
                      {meta.label}
                    </span>
                    {!isDefault && (
                      <span className="text-[10px] px-1 py-0.5 rounded" style={{ color: C.accent, backgroundColor: C.accentSubtle }}>
                        custom
                      </span>
                    )}
                  </div>
                  {meta.desc && (
                    <div className="text-xs mb-2.5" style={{ color: "var(--color-text-muted)" }}>{meta.desc}</div>
                  )}
                  <div className="grid grid-cols-3 gap-2">
                    {LEVEL_OPTIONS.map((opt) => {
                      const isActive = current === opt.value;
                      return (
                        <button
                          key={opt.value}
                          onClick={() => handleChange(action, opt.value)}
                          disabled={updateMutation.isPending}
                          className="flex items-center justify-center gap-1 h-8 rounded-md text-xs font-medium transition-all duration-200 cursor-pointer disabled:opacity-50"
                          style={{
                            backgroundColor: isActive ? `color-mix(in srgb, ${opt.color} 20%, transparent)` : "transparent",
                            color: isActive ? opt.color : "var(--color-text-muted)",
                            border: `1px solid ${isActive ? opt.color : "rgba(255,255,255,0.06)"}`,
                          }}
                        >
                          {isActive && <Check size={11} />}
                          <span>{opt.label}</span>
                        </button>
                      );
                    })}
                  </div>
                </div>

                {/* Desktop: grid layout */}
                <div
                  className="hidden sm:grid items-center gap-3"
                  style={{ gridTemplateColumns: "1fr 80px 80px 80px" }}
                >
                  <div className="min-w-0">
                    <div className="text-sm font-medium" style={{ color: "var(--color-text-primary)" }}>
                      {meta.label}
                      {!isDefault && (
                        <span className="ml-1.5 text-[10px] px-1 py-0.5 rounded" style={{ color: C.accent, backgroundColor: C.accentSubtle }}>
                          custom
                        </span>
                      )}
                    </div>
                    {meta.desc && (
                      <div className="text-xs mt-0.5" style={{ color: "var(--color-text-muted)" }}>{meta.desc}</div>
                    )}
                  </div>
                  {LEVEL_OPTIONS.map((opt) => {
                    const isActive = current === opt.value;
                    return (
                      <button
                        key={opt.value}
                        onClick={() => handleChange(action, opt.value)}
                        disabled={updateMutation.isPending}
                        className="flex items-center justify-center h-7 rounded-md text-xs font-medium transition-all duration-200 cursor-pointer disabled:opacity-50"
                        style={{
                          backgroundColor: isActive ? `color-mix(in srgb, ${opt.color} 20%, transparent)` : "transparent",
                          color: isActive ? opt.color : "var(--color-text-muted)",
                          border: `1px solid ${isActive ? opt.color : "rgba(255,255,255,0.06)"}`,
                        }}
                      >
                        {isActive && <Check size={12} className="mr-0.5" />}
                        {opt.value}
                      </button>
                    );
                  })}
                </div>
              </div>
            );
          })}
        </div>

        <div className="mt-4 text-xs" style={{ color: "var(--color-text-muted)" }}>
          L1 = Auto | L2 = Notify | L3 = Approve
        </div>
      </div>
    </SectionMotion>
  );
}

// ── Intelligence Section (Admin only) ─────────────────────────────────────────

function IntelligenceSection() {
  const queryClient = useQueryClient();
  const [config, setConfig] = useState<IntelligenceConfig | null>(null);
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
      <SectionHeader
        title="Intelligence Service"
        description="Configuration for automatic analysis and LLM distillation."
      />

      {error && <ErrorBanner message={error} />}

      <div className="space-y-6">
        {/* Enabled Toggle */}
        <div className="mc-card p-5 flex items-center justify-between" style={cardStyle}>
          <div>
            <span className="text-sm font-medium" style={{ color: "var(--color-text-primary)" }}>
              Service Active
            </span>
            <p className="text-xs mt-0.5" style={{ color: "var(--color-text-muted)" }}>
              Run periodic analysis in the background.
            </p>
          </div>
          <button
            onClick={() => update({ enabled: !config.enabled })}
            className="relative w-11 h-6 rounded-full transition-colors cursor-pointer"
            style={{
              backgroundColor: config.enabled ? C.accent : "rgba(255, 255, 255, 0.06)",
              border: config.enabled ? "none" : "1px solid rgba(255, 255, 255, 0.08)",
            }}
          >
            <span
              className="absolute top-0.5 w-5 h-5 rounded-full bg-white transition-transform"
              style={{ left: config.enabled ? "calc(100% - 22px)" : "2px" }}
            />
          </button>
        </div>

        {/* Analyse */}
        <div className="mc-card p-5 space-y-4" style={cardStyle}>
          <h3 className="text-sm font-medium" style={{ color: "var(--color-text-primary)" }}>
            Analysis
          </h3>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <div>
              <FieldLabel>Analysis Interval (seconds)</FieldLabel>
              <InputField
                type="number"
                value={String(config.interval_seconds)}
                onChange={(v) => update({ interval_seconds: Math.max(60, parseInt(v) || 60) })}
              />
              <p className="text-xs mt-1" style={{ color: "var(--color-text-muted)" }}>Min. 60s</p>
            </div>
            <div>
              <FieldLabel>Analysis Window (days)</FieldLabel>
              <InputField
                type="number"
                value={String(config.analysis_window_days)}
                onChange={(v) => update({ analysis_window_days: Math.max(1, parseInt(v) || 1) })}
              />
            </div>
          </div>
        </div>

        {/* Ollama / LLM */}
        <div className="mc-card p-5 space-y-4" style={cardStyle}>
          <h3 className="text-sm font-medium" style={{ color: "var(--color-text-primary)" }}>
            Ollama / LLM
          </h3>
          <div>
            <FieldLabel>Model</FieldLabel>
            <InputField
              value={config.ollama_model}
              onChange={(v) => update({ ollama_model: v })}
              placeholder="qwen2.5-coder:14b"
            />
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <div>
              <FieldLabel>Temperature</FieldLabel>
              <InputField
                type="number"
                value={String(config.temperature)}
                onChange={(v) => {
                  const n = parseFloat(v);
                  if (!isNaN(n)) update({ temperature: Math.min(1, Math.max(0, n)) });
                }}
              />
              <p className="text-xs mt-1" style={{ color: "var(--color-text-muted)" }}>0.0 - 1.0</p>
            </div>
            <div>
              <FieldLabel>Max Tokens</FieldLabel>
              <InputField
                type="number"
                value={String(config.max_tokens)}
                onChange={(v) => update({ max_tokens: Math.min(8192, Math.max(100, parseInt(v) || 100)) })}
              />
              <p className="text-xs mt-1" style={{ color: "var(--color-text-muted)" }}>100 - 8192</p>
            </div>
          </div>
          <div>
            <FieldLabel>System Prompt</FieldLabel>
            <textarea
              aria-label="System prompt"
              value={config.system_prompt}
              onChange={(e) => update({ system_prompt: e.target.value })}
              rows={6}
              placeholder="Leave empty for default prompt"
              className={cn(inputBaseClasses, "resize-y")}
              style={{
                backgroundColor: C.bgDeep,
                borderWidth: 1,
                borderStyle: "solid",
                borderColor: "rgba(255, 255, 255, 0.08)",
                color: "var(--color-text-primary)",
              }}
              onFocus={(e) => {
                e.currentTarget.style.borderColor = C.borderAccent;
              }}
              onBlur={(e) => {
                e.currentTarget.style.borderColor = "rgba(255, 255, 255, 0.08)";
              }}
            />
            <p className="text-xs mt-1" style={{ color: "var(--color-text-muted)" }}>
              Leave empty for default prompt. Analysis data is appended automatically.
            </p>
          </div>
        </div>

        {/* Schwellenwerte */}
        <div className="mc-card p-5 space-y-4" style={cardStyle}>
          <h3 className="text-sm font-medium" style={{ color: "var(--color-text-primary)" }}>
            Thresholds
          </h3>
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
            <div>
              <FieldLabel>Outlier Multiplier</FieldLabel>
              <InputField
                type="number"
                value={String(config.outlier_multiplier)}
                onChange={(v) => {
                  const n = parseFloat(v);
                  if (!isNaN(n) && n > 1) update({ outlier_multiplier: n });
                }}
              />
              <p className="text-xs mt-1" style={{ color: "var(--color-text-muted)" }}>&gt;1.0x</p>
            </div>
            <div>
              <FieldLabel>Success Rate Min. (%)</FieldLabel>
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
              <FieldLabel>Failure Max.</FieldLabel>
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
              color: triggerSuccess ? "white" : "var(--color-text-primary)",
              border: triggerSuccess ? "none" : "1px solid rgba(255, 255, 255, 0.08)",
            }}
          >
            {triggerMutation.isPending ? (
              <Loader2 size={14} className="animate-spin" />
            ) : triggerSuccess ? (
              <Check size={14} />
            ) : (
              <Play size={14} />
            )}
            {triggerSuccess ? "Analysis started" : "Analyze Now"}
          </button>
        </div>
      </div>
    </SectionMotion>
  );
}

// ── API Keys Section (Admin only) ─────────────────────────────────────────────

function ApiKeysSection() {
  const queryClient = useQueryClient();
  const [addingKey, setAddingKey] = useState<string | null>(null);
  const [newValue, setNewValue] = useState("");
  const [editingKey, setEditingKey] = useState<string | null>(null);
  const [editValue, setEditValue] = useState("");
  const [showValue, setShowValue] = useState<string | null>(null);

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

  return (
    <SectionMotion sectionKey="apikeys">
      <SectionHeader
        title="API Keys"
        description="API keys for AI providers and integrations. All keys are stored encrypted. See /runtimes for provider health (LM Studio / Ollama / vLLM / Anthropic live status)."
      />

      {isLoading ? (
        <div className="flex items-center justify-center py-12">
          <Loader2 className="animate-spin" size={20} style={{ color: "var(--color-text-muted)" }} />
        </div>
      ) : (
        <div className="space-y-3">
          {(providers ?? []).map((tmpl) => {
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
                            ? "rgba(0, 204, 136, 0.1)"
                            : "rgba(255, 255, 255, 0.04)",
                          color: isSet ? C.online : "var(--color-text-muted)",
                        }}
                      >
                        {isSet ? "Set" : "Not set"}
                      </span>
                    </div>
                    <p className="text-xs mt-0.5" style={{ color: "var(--color-text-muted)" }}>
                      {tmpl.description}
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
                          {isEditing ? "Cancel" : "Change"}
                        </button>
                        <button
                          onClick={() => {
                            if (confirm(`Really delete ${tmpl.label}?`)) {
                              deleteMutation.mutate(tmpl.key);
                            }
                          }}
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
                            ? "rgba(255, 255, 255, 0.04)"
                            : C.accentSubtle,
                          color: isAdding ? "var(--color-text-muted)" : C.accent,
                        }}
                      >
                        {isAdding ? <X size={12} /> : <Plus size={12} />}
                        {isAdding ? "Cancel" : "Add"}
                      </button>
                    )}
                  </div>
                </div>

                {/* Add form */}
                {isAdding && (
                  <div
                    className="mt-3 pt-3 border-t flex gap-2"
                    style={{ borderColor: "rgba(255, 255, 255, 0.06)" }}
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
                      className="shrink-0 px-3 py-2 rounded-lg text-xs font-medium cursor-pointer disabled:opacity-40 text-white"
                      style={{
                        background: `linear-gradient(135deg, ${C.accent}, ${C.accentHover})`,
                      }}
                    >
                      {createMutation.isPending ? (
                        <Loader2 size={12} className="animate-spin" />
                      ) : (
                        "Save"
                      )}
                    </button>
                  </div>
                )}

                {/* Edit form */}
                {isEditing && (
                  <div
                    className="mt-3 pt-3 border-t flex gap-2"
                    style={{ borderColor: "rgba(255, 255, 255, 0.06)" }}
                  >
                    <InputField
                      type={showValue === tmpl.key ? "text" : "password"}
                      value={editValue}
                      onChange={setEditValue}
                      placeholder="New value..."
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
                      className="shrink-0 px-3 py-2 rounded-lg text-xs font-medium cursor-pointer disabled:opacity-40 text-white"
                      style={{
                        background: `linear-gradient(135deg, ${C.accent}, ${C.accentHover})`,
                      }}
                    >
                      {updateMutation.isPending ? (
                        <Loader2 size={12} className="animate-spin" />
                      ) : (
                        "Update"
                      )}
                    </button>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </SectionMotion>
  );
}

// ── GitHub Section (ADR-055, admin only) ──────────────────────────────────────

function GithubSourceBadge({ source }: { source: "vault" | "env" | null }) {
  if (!source) return null;
  return (
    <span
      className="text-[10px] px-1.5 py-0.5 rounded uppercase"
      style={{ backgroundColor: "rgba(255, 255, 255, 0.04)", color: "var(--color-text-muted)" }}
    >
      {source === "vault" ? "App" : ".env"}
    </span>
  );
}

function GithubSection() {
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
      setSaveMessage("Saved.");
      await queryClient.invalidateQueries({ queryKey: ["github-status"] });
    },
    onError: (err) => {
      setSaveMessage(null);
      setSaveError(err instanceof Error ? err.message : "Failed to save.");
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
      setSaveMessage("Nothing changed.");
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
        error: err instanceof Error ? err.message : "Test connection failed.",
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
      ? "Connected"
      : connected === false
      ? "Connection failed"
      : effective?.configured
      ? "Not tested"
      : "Not connected";

  return (
    <SectionMotion sectionKey="github">
      <SectionHeader
        title="GitHub"
        description="Connect a GitHub owner + token so agents can create repos, branch per task, and open pull requests."
      />

      <p className="text-sm mb-4" style={{ color: "var(--color-text-secondary)" }}>
        Once connected, MC can create a private repo per project and a branch per task,
        agents open PRs directly from their workspace, and each repo can carry its own
        working rules that are included in every dispatch. Manage the registry under{" "}
        <Link href="/repos" className="underline" style={{ color: C.accent }}>
          Repos
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
              <span style={{ color: "var(--color-text-muted)" }}>Owner</span>
              <span className="font-mono" style={{ color: "var(--color-text-primary)" }}>
                {effective?.owner ?? "—"}
              </span>
              <GithubSourceBadge source={effective?.owner_source ?? null} />
            </div>

            <div className="flex items-center gap-2 text-xs">
              <span style={{ color: "var(--color-text-muted)" }}>Token</span>
              <span className="font-mono" style={{ color: "var(--color-text-primary)" }}>
                {effective?.token_set ? "Set ••••" : "Not set"}
              </span>
              <GithubSourceBadge source={effective?.token_source ?? null} />
            </div>

            {connected !== null && (
              <div className="pt-2 mt-1 space-y-1 text-xs" style={{ borderTop: `1px solid ${C.borderSubtle}`, color: "var(--color-text-muted)" }}>
                {effective?.login && (
                  <div>authenticated as <span className="font-mono" style={{ color: "var(--color-text-secondary)" }}>{effective.login}</span></div>
                )}
                {effective?.owner_type && (
                  <div>owner type <span className="font-mono" style={{ color: "var(--color-text-secondary)" }}>{effective.owner_type}</span></div>
                )}
                {effective?.rate_limit_total != null && (
                  <div>
                    rate limit{" "}
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
              style={{ background: "rgba(255, 255, 255, 0.04)", color: "var(--color-text-secondary)" }}
            >
              {probing ? "Testing (up to 15s)…" : "Test connection"}
            </button>
          </div>

          {/* Form */}
          <div className="mc-card p-4 space-y-3" style={cardStyle}>
            <div>
              <FieldLabel>Owner</FieldLabel>
              <InputField
                value={owner}
                onChange={(v) => { setOwner(v); setOwnerTouched(true); }}
                placeholder="your-github-user-or-org"
                ariaLabel="GitHub owner"
              />
            </div>
            <div>
              <FieldLabel>Token</FieldLabel>
              <InputField
                type="password"
                value={token}
                onChange={setToken}
                placeholder={status?.token_set ? "unchanged — paste to rotate" : "ghp_..."}
                ariaLabel="GitHub token"
              />
            </div>

            {saveError && (
              <p className="text-xs rounded-lg px-3 py-2" style={{ color: STATUS_TEXT.error, backgroundColor: "rgba(239, 68, 68, 0.08)", border: "1px solid rgba(239, 68, 68, 0.15)" }}>
                {saveError}
              </p>
            )}
            {saveMessage && (
              <p className="text-xs rounded-lg px-3 py-2 flex items-center gap-1.5" style={{ color: C.online, backgroundColor: "rgba(43, 154, 74, 0.1)" }}>
                <Check size={12} /> {saveMessage}
              </p>
            )}

            <button
              onClick={handleSave}
              disabled={saveMutation.isPending}
              className="text-xs px-3 py-2 rounded-lg font-medium cursor-pointer disabled:opacity-40 text-white"
              style={{ background: `linear-gradient(135deg, ${C.accent}, ${C.accentHover})` }}
            >
              {saveMutation.isPending ? <Loader2 size={12} className="animate-spin" /> : "Save"}
            </button>
          </div>
        </div>
      )}
    </SectionMotion>
  );
}

// ── Users Section (Admin only) ────────────────────────────────────────────────

function UsersSection() {
  const queryClient = useQueryClient();
  const [showCreateForm, setShowCreateForm] = useState(false);

  const { data: users, isLoading } = useQuery({
    queryKey: ["admin-users"],
    queryFn: api.auth.users.list,
  });

  return (
    <SectionMotion sectionKey="users">
      <SectionHeader
        title="Manage Users"
        description="Create users, assign roles, and manage accounts."
      />

      {/* Create button */}
      <div className="flex justify-end mb-4">
        <button
          onClick={() => setShowCreateForm(!showCreateForm)}
          className="flex items-center gap-2 px-3 py-1.5 rounded-lg text-xs font-medium transition-all duration-200 cursor-pointer text-white"
          style={{
            background: showCreateForm
              ? "transparent"
              : `linear-gradient(135deg, ${C.accent}, ${C.accentHover})`,
            color: showCreateForm ? "var(--color-text-secondary)" : "white",
            border: showCreateForm ? "1px solid rgba(255, 255, 255, 0.08)" : "none",
          }}
        >
          {showCreateForm ? (
            <>
              <X size={12} /> Cancel
            </>
          ) : (
            <>
              <Plus size={12} /> New User
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
          <FieldLabel>Name</FieldLabel>
          <InputField value={name} onChange={setName} placeholder="Name" />
        </div>
        <div>
          <FieldLabel>Email</FieldLabel>
          <InputField value={email} onChange={setEmail} placeholder="user@example.com" />
        </div>
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        <div>
          <FieldLabel>Password</FieldLabel>
          <InputField
            type="password"
            value={password}
            onChange={setPassword}
            placeholder="Min. 6 characters"
          />
        </div>
        <div>
          <FieldLabel>Role</FieldLabel>
          <select
            value={role}
            onChange={(e) => setRole(e.target.value)}
            aria-label="Select role"
            className={inputBaseClasses}
            style={{
              backgroundColor: C.bgDeep,
              borderWidth: 1,
              borderStyle: "solid",
              borderColor: "rgba(255, 255, 255, 0.08)",
              color: "var(--color-text-primary)",
              cursor: "pointer",
            }}
            onFocus={(e) => {
              e.currentTarget.style.borderColor = C.borderAccent;
            }}
            onBlur={(e) => {
              e.currentTarget.style.borderColor = "rgba(255, 255, 255, 0.08)";
            }}
          >
            <option value="admin">Admin</option>
            <option value="operator">Operator</option>
            <option value="viewer">Viewer</option>
          </select>
        </div>
      </div>

      <SaveButton
        onClick={() => mutation.mutate()}
        loading={mutation.isPending}
        disabled={!email.trim() || !name.trim() || password.length < 6}
        label="Create User"
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
    viewer: { bg: "rgba(255, 255, 255, 0.04)", text: "var(--color-text-muted)" },
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
            backgroundColor: "rgba(255, 255, 255, 0.04)",
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
                  backgroundColor: "rgba(255, 255, 255, 0.04)",
                  color: "var(--color-text-muted)",
                }}
              >
                You
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
                Deactivated
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
                aria-label="Change role"
                value={role}
                onChange={(e) => setRole(e.target.value)}
                className="rounded px-2 py-1 text-xs outline-none cursor-pointer"
                style={{
                  backgroundColor: C.bgDeep,
                  border: "1px solid rgba(255, 255, 255, 0.08)",
                  color: "var(--color-text-primary)",
                }}
              >
                <option value="admin">Admin</option>
                <option value="operator">Operator</option>
                <option value="viewer">Viewer</option>
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
                className="px-2 py-1 rounded text-xs font-medium cursor-pointer transition-colors text-white"
                style={{ background: `linear-gradient(135deg, ${C.accent}, ${C.accentHover})` }}
              >
                {updateMutation.isPending ? (
                  <Loader2 size={12} className="animate-spin" />
                ) : (
                  "Save"
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
                Cancel
              </button>
            </>
          ) : (
            <>
              <button
                onClick={() => setEditing(true)}
                className="px-2 py-1 rounded text-xs cursor-pointer transition-colors"
                style={{ color: "var(--color-text-secondary)" }}
              >
                Edit
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
                {user.is_active ? "Deactivate" : "Activate"}
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
        title="Keyboard Shortcuts"
        description="Vim-style chord shortcuts: press g followed by a letter"
      />

      <div className="mc-card p-6" style={cardStyle}>
        <div className="space-y-1">
          {SHORTCUTS.map((shortcut, i) => (
            <motion.div
              key={shortcut.description}
              custom={i}
              initial="initial"
              animate="animate"
              variants={stagger}
              className="flex items-center justify-between py-2.5 px-3 rounded-lg transition-colors"
              style={{
                backgroundColor:
                  i % 2 === 0 ? "rgba(255, 255, 255, 0.02)" : "transparent",
              }}
            >
              <span className="text-sm" style={{ color: "var(--color-text-body)" }}>
                {shortcut.description}
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
                        backgroundColor: "rgba(255, 255, 255, 0.05)",
                        border: "1px solid rgba(255, 255, 255, 0.08)",
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
  const { data: version } = useQuery({
    queryKey: ["system-version"],
    queryFn: api.system.version,
    staleTime: 60 * 60 * 1000,
  });
  return (
    <SectionMotion sectionKey="about">
      <SectionHeader title="About" description="System information and links." />

      <div className="space-y-6">
        {/* System info */}
        <div className="mc-card p-6" style={cardStyle}>
          <h3
            className="text-sm font-semibold mb-6"
            style={{ color: "var(--color-text-primary)" }}
          >
            System
          </h3>
          <div className="space-y-4">
            {[
              { label: "Version", value: version?.current ?? "…" },
              { label: "Frontend", value: "Next.js 15 + TypeScript + Tailwind v4" },
              { label: "Backend", value: "FastAPI + PostgreSQL + Redis" },
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
                  Update available: {version.latest}
                </span>
                <a
                  href={version.release_url}
                  target="_blank"
                  rel="noreferrer"
                  className="text-sm font-mono hover:underline"
                  style={{ color: "var(--color-accent)" }}
                >
                  Release-Notes →
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
            Links
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
          Built with care by the Operator & Claude
        </div>
      </div>
    </SectionMotion>
  );
}

// ── Main Page ─────────────────────────────────────────────────────────────────

function SettingsContent() {
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
        style={{ borderBottom: "1px solid rgba(255, 255, 255, 0.04)" }}
      >
        <h1 className="text-heading-page">Settings</h1>
        <p className="text-sm mt-1" style={{ color: "var(--color-text-secondary)" }}>
          Profile, security, system configuration
        </p>
      </motion.div>

      <div className="flex-1 flex flex-col md:flex-row overflow-hidden">
        {/* Left: Section Nav (glass sidebar) */}
        <motion.nav
          initial={{ opacity: 0, x: -8 }}
          animate={{ opacity: 1, x: 0 }}
          transition={{ delay: 0.1, duration: 0.3, ease: [0.16, 1, 0.3, 1] }}
          className="w-full md:w-56 shrink-0 border-b md:border-b-0 md:border-r overflow-x-auto md:overflow-y-auto py-3 tab-strip-nav"
          style={{
            borderColor: "rgba(255, 255, 255, 0.04)",
            backgroundColor: "rgba(255, 255, 255, 0.01)",
          }}
        >
          <ul className="flex md:flex-col gap-1 md:gap-0.5 px-2 min-w-max md:min-w-0">
            {visibleSections.map((section) => {
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
                        e.currentTarget.style.backgroundColor = "rgba(255, 255, 255, 0.03)";
                      }
                    }}
                    onMouseLeave={(e) => {
                      if (!isActive) {
                        e.currentTarget.style.backgroundColor = "transparent";
                      }
                    }}
                  >
                    {/* Active state = accent-subtle surface + teal icon (DESIGN.md
                        navigation pattern) — no side-stripe indicator on top. */}
                    <Icon
                      size={16}
                      style={{
                        color: isActive ? C.accent : undefined,
                      }}
                    />
                    <span className="whitespace-nowrap">{section.label}</span>
                  </button>
                </li>
              );
            })}
          </ul>
        </motion.nav>

        {/* Right: Section Content */}
        <div className="flex-1 overflow-y-auto p-4 md:p-6 min-w-0">
          <div className="max-w-2xl min-w-0">
            <AnimatePresence mode="wait">
              {activeSection === "profile" && <ProfileSection />}
              {activeSection === "security" && <SecuritySection />}
              {activeSection === "autonomy" && isAdmin && <AutonomySection />}
              {activeSection === "intelligence" && isAdmin && <IntelligenceSection />}
              {activeSection === "apikeys" && isAdmin && <ApiKeysSection />}
              {activeSection === "github" && isAdmin && <GithubSection />}
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
