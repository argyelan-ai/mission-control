"use client";

import { useState, useCallback, useMemo, useEffect, Fragment } from "react";
import { useLocale, useTranslations } from "next-intl";
import AppShell from "@/components/layout/AppShell";
import { useParams, useRouter } from "next/navigation";
import { useQuery, useQueryClient, useMutation } from "@tanstack/react-query";
import { motion, AnimatePresence } from "framer-motion";
import ReactMarkdown from "react-markdown";
import Link from "next/link";
import {
  ArrowLeft, Zap, RotateCcw, Cloud, Trash2, Save,
  Loader2, Activity, Settings,
  AlertTriangle, Search,
  Download, Power, PowerOff, Key, ExternalLink,
  WifiOff, Undo2, Plus, Minus, CheckCircle, XCircle,
  Brain, Wrench, FileText, Play, Pause, Server,
  HardDrive, FolderArchive, RefreshCw, Package, Box,
} from "lucide-react";
import { cn, contextPercent, contextColor, timeAgo } from "@/lib/utils";
import { C, STATUS } from "@/lib/colors";
import { api } from "@/lib/api";
import { useAgentStream } from "@/lib/sse";
import { notify } from "@/lib/notify";
import { GlassCard } from "@/components/shared/GlassCard";
import { SpotlightCard } from "@/components/shared/SpotlightCard";
import { StatusDot } from "@/components/shared/StatusDot";
import { Pill } from "@/components/shared/Pill";
import { ActivityFeed } from "@/components/shared/ActivityFeed";
import { SkillBadges } from "@/components/agent/AgentCard";
import { RuntimePill, RUNTIME_TYPE_COLOR } from "@/components/shared/RuntimePill";
import { RuntimeSwitchModal } from "@/components/shared/RuntimeSwitchModal";
import { ConfirmDialog } from "@/components/shared/ConfirmDialog";
import type {
  Agent, AgentMetrics, ActivityEvent as ActivityEventType,
  OpenClawSkill, AgentSkillsResponse,
  ScheduledJob, CustomSkill, CliPlugin,
} from "@/lib/types";
import { MCPServerMatrix } from "@/components/mcp/MCPServerMatrix";
import { AgentActions } from "@/components/agent/AgentActions";
import { EntityIcon } from "@/components/shared/EntityIcon";
import { groupRuntimesByProvider } from "@/lib/groupRuntimes";

// ── Types ─────────────────────────────────────────────────────────────────────

type Tab = "overview" | "skills" | "config" | "memory" | "local-memory" | "mcp";

// labelKey pattern (docs/i18n.md): resolved via t() at the render site.
const TABS: { key: Tab; labelKey: string; icon: typeof Activity }[] = [
  { key: "overview", labelKey: "detail.tabOverview", icon: Settings },
  { key: "skills", labelKey: "detail.tabSkills", icon: Wrench },
  { key: "mcp", labelKey: "detail.tabMcp", icon: Server },
  { key: "config", labelKey: "detail.tabConfig", icon: FileText },
  { key: "memory", labelKey: "detail.tabMemory", icon: Brain },
  { key: "local-memory", labelKey: "detail.tabLocalMemory", icon: FolderArchive },
];

const CONFIG_FILES = [
  { key: "soul_md", label: "SOUL.md", readonly: false },
  { key: "rules_md", label: "RULES.md", readonly: false },
  { key: "tools_md", label: "TOOLS.md", readonly: false },
] as const;

const HEARTBEAT_INTERVALS = [
  { value: "30s", label: "30s" },
  { value: "1m", label: "1m" },
  { value: "2m", label: "2m" },
  { value: "5m", label: "5m" },
  { value: "10m", label: "10m" },
];

// ── Status Mapping ─────────────────────────────────────────────────────────────

type DotStatus = "online" | "busy" | "idle" | "offline" | "error" | "warning";

function agentStatusToDot(status: string): DotStatus {
  switch (status) {
    case "online": return "online";
    case "busy": return "busy";
    case "error": return "error";
    case "restarting": return "warning";
    case "idle": return "idle";
    default: return "offline";
  }
}

const PROVISION_CONFIG: Record<string, { labelKey: string; color: string }> = {
  local: { labelKey: "provLocal", color: C.textDim },
  provisioning: { labelKey: "provProvisioning", color: C.warning },
  provisioned: { labelKey: "provLive", color: C.online },
  error: { labelKey: "provError", color: C.error },
};

// RuntimePill + RUNTIME_TYPE_COLOR are imported from
// @/components/shared/RuntimePill at the top of the file (Phase 15 T3.4).

// ── Skills Editor (embedded) ────────────────────────────────────────────────

const SKILL_STATUS_CONFIG: Record<string, { color: string; labelKey: string; icon: typeof CheckCircle }> = {
  ready: { color: C.online, labelKey: "detail.skillReady", icon: CheckCircle },
  missing_bin: { color: C.warning, labelKey: "detail.skillMissingBin", icon: AlertTriangle },
  missing_env: { color: C.warning, labelKey: "detail.skillMissingEnv", icon: Key },
  disabled: { color: C.textDim, labelKey: "detail.skillDisabled", icon: PowerOff },
  not_installed: { color: C.error, labelKey: "detail.skillNotInstalled", icon: XCircle },
};

function SkillStatusIcon({ status }: { status: string }) {
  const cfg = SKILL_STATUS_CONFIG[status] ?? SKILL_STATUS_CONFIG.not_installed;
  const Icon = cfg.icon;
  return <Icon size={13} style={{ color: cfg.color }} />;
}

function SkillRow({
  skill,
  isActive,
  pendingChange,
  onToggle,
}: {
  skill: OpenClawSkill;
  isActive?: boolean;
  pendingChange?: "add" | "remove";
  onToggle?: (key: string) => void;
}) {
  const t = useTranslations("agents");
  const qc = useQueryClient();
  const cfg = SKILL_STATUS_CONFIG[skill.status] ?? SKILL_STATUS_CONFIG.not_installed;

  const installMutation = useMutation({
    mutationFn: (installId: string) => api.skills.install(skill.key, installId),
    onSuccess: () => {
      notify.success(t("detail.installing", { name: skill.name }));
      qc.invalidateQueries({ queryKey: ["openclaw-skills"] });
    },
    onError: (e: Error) => notify.error(e.message),
  });

  const toggleMutation = useMutation({
    mutationFn: (enabled: boolean) => api.skills.update(skill.key, { enabled }),
    onSuccess: (_, enabled) => {
      notify.success(enabled ? t("detail.skillEnabledNotify", { name: skill.name }) : t("detail.skillDisabledNotify", { name: skill.name }));
      qc.invalidateQueries({ queryKey: ["openclaw-skills"] });
    },
    onError: (e: Error) => notify.error(e.message),
  });

  const borderColor = pendingChange === "add"
    ? `${C.online}66`
    : pendingChange === "remove"
    ? `${C.error}66`
    : skill.status === "ready"
    ? "var(--color-border)"
    : `${cfg.color}33`;

  const bgTint = pendingChange === "add"
    ? `${C.online}08`
    : pendingChange === "remove"
    ? `${C.error}08`
    : undefined;

  return (
    <motion.div
      layout
      initial={{ opacity: 0, y: 4 }}
      animate={{ opacity: pendingChange === "remove" ? 0.5 : 1, y: 0 }}
      className="flex items-center justify-between gap-3 py-2.5 px-3 rounded-xl transition-colors"
      style={{
        backgroundColor: bgTint ?? "var(--color-bg-surface)",
        border: `1px solid ${borderColor}`,
      }}
    >
      <div className="flex items-center gap-2.5 min-w-0 flex-1">
        <SkillStatusIcon status={skill.status} />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5">
            <span
              className="text-sm font-medium truncate"
              style={{
                color: "var(--color-text-primary)",
                textDecoration: pendingChange === "remove" ? "line-through" : undefined,
              }}
            >
              {skill.emoji && <EntityIcon value={skill.emoji} size={12} className="mr-1" />}
              {skill.name}
            </span>
            <span className="text-[10px] px-1.5 py-0.5 rounded-sm font-mono shrink-0" style={{ color: cfg.color, backgroundColor: `${cfg.color}18` }}>
              {t(cfg.labelKey)}
            </span>
            {pendingChange && (
              <span
                className="text-[10px] px-1.5 py-0.5 rounded-sm font-mono shrink-0 font-medium"
                style={{
                  color: pendingChange === "add" ? C.online : C.error,
                  backgroundColor: pendingChange === "add" ? `${C.online}18` : `${C.error}18`,
                }}
              >
                {pendingChange === "add" ? t("detail.pendingNew") : t("detail.pendingRemoved")}
              </span>
            )}
            {skill.source !== "bundled" && (
              <span className="text-[10px] px-1.5 py-0.5 rounded-sm font-mono" style={{ color: "var(--color-text-muted)", backgroundColor: "var(--color-bg-elevated)" }}>
                {skill.source}
              </span>
            )}
          </div>
          {skill.description && (
            <div className="text-xs mt-0.5 truncate" style={{ color: "var(--color-text-muted)" }}>
              {skill.description}
            </div>
          )}
        </div>
      </div>

      <div className="flex items-center gap-1.5 shrink-0">
        {(skill.status === "not_installed" || skill.status === "missing_bin") && skill.install && skill.install.length > 0 && (
          skill.install.map((opt) => (
            <button
              key={opt.id}
              onClick={() => installMutation.mutate(opt.id)}
              disabled={installMutation.isPending}
              className="flex items-center gap-1 text-[11px] px-2 py-1 rounded-lg cursor-pointer transition-colors"
              style={{
                backgroundColor: C.accentSubtle,
                color: C.accent,
                border: `1px solid ${C.borderAccent}`,
              }}
            >
              {installMutation.isPending ? <Loader2 size={11} className="animate-spin" /> : <Download size={11} />}
              {opt.label || opt.kind}
            </button>
          ))
        )}

        {skill.status === "ready" && !onToggle && (
          <button
            onClick={() => toggleMutation.mutate(false)}
            disabled={toggleMutation.isPending}
            className="flex items-center gap-1 text-[11px] px-2 py-1 rounded-lg cursor-pointer transition-colors"
            style={{ color: "var(--color-text-muted)", backgroundColor: "var(--color-bg-elevated)" }}
            title={t("detail.disableSkill")}
          >
            {toggleMutation.isPending ? <Loader2 size={11} className="animate-spin" /> : <PowerOff size={11} />}
          </button>
        )}

        {skill.status === "disabled" && !onToggle && (
          <button
            onClick={() => toggleMutation.mutate(true)}
            disabled={toggleMutation.isPending}
            className="flex items-center gap-1 text-[11px] px-2 py-1 rounded-lg cursor-pointer transition-colors"
            style={{ color: C.online, backgroundColor: `${C.online}1F` }}
            title={t("detail.enableSkill")}
          >
            {toggleMutation.isPending ? <Loader2 size={11} className="animate-spin" /> : <Power size={11} />}
            {t("detail.enable")}
          </button>
        )}

        {skill.homepage && (
          <a
            href={skill.homepage}
            target="_blank"
            rel="noopener noreferrer"
            className="p-1 rounded transition-colors"
            style={{ color: "var(--color-text-muted)" }}
            title={t("detail.homepage")}
          >
            <ExternalLink size={12} />
          </a>
        )}

        {onToggle && (
          <button
            onClick={() => onToggle(skill.key)}
            className="flex items-center gap-1 text-[11px] px-2 py-1 rounded-lg cursor-pointer transition-colors"
            style={{
              backgroundColor: isActive
                ? pendingChange === "remove" ? `${C.error}18` : `${C.accent}26`
                : pendingChange === "add" ? `${C.online}18` : "var(--color-bg-elevated)",
              color: isActive
                ? pendingChange === "remove" ? C.error : C.accent
                : pendingChange === "add" ? C.online : "var(--color-text-muted)",
              border: isActive && !pendingChange
                ? `1px solid ${C.borderAccent}`
                : "1px solid transparent",
            }}
          >
            {isActive && !pendingChange && <Minus size={11} />}
            {pendingChange === "remove" && <Undo2 size={11} />}
            {pendingChange === "add" && <Undo2 size={11} />}
            {!isActive && !pendingChange && <Plus size={11} />}
            {pendingChange ? t("detail.undo") : isActive ? t("detail.remove") : t("detail.add")}
          </button>
        )}
      </div>
    </motion.div>
  );
}

// ── Host-Agent Skills View (read-only) ───────────────────────────────────────
// Host agents (Boss, Hermes, Jarvis) are launchd-managed on the Mac and read
// their skills + CLI plugins directly from the shared ~/.mc cache via the
// filesystem — there is no gateway and no per-container settings.json to
// rewrite, so assignment isn't editable here (it's managed on the host /
// via the Skills page). This view shows what the agent actually has access to.
// Pre-fix this branch rendered a dead "OpenClaw Gateway nicht verbunden" error
// (gateway retired in v0.9, ADR-039).

function HostSkillRow({ name, meta, badge, badgeColor }: {
  name: string;
  meta?: string;
  badge?: string;
  badgeColor?: string;
}) {
  return (
    <div
      className="flex items-center gap-3 py-2.5 px-3 rounded-xl"
      style={{ backgroundColor: "var(--color-bg-surface)", border: "1px solid var(--color-border)" }}
    >
      <span className="w-[3px] self-stretch rounded-full shrink-0" style={{ background: badgeColor ?? C.online, minHeight: 18 }} />
      <div className="min-w-0 flex-1">
        <div className="text-sm font-medium truncate" style={{ color: "var(--color-text-primary)" }}>{name}</div>
        {meta && <div className="text-xs mt-0.5 truncate" style={{ color: "var(--color-text-muted)" }}>{meta}</div>}
      </div>
      {badge && (
        <span
          className="text-[10px] px-1.5 py-0.5 rounded-sm font-mono shrink-0"
          style={{ color: badgeColor ?? C.online, backgroundColor: `${badgeColor ?? C.online}18` }}
        >
          {badge}
        </span>
      )}
    </div>
  );
}

function HostSkillsView({
  agentName,
  agentRuntime,
  data,
}: {
  agentName: string;
  agentRuntime: string;
  data: AgentSkillsResponse | undefined;
}) {
  const t = useTranslations("agents.detail");
  const [search, setSearch] = useState("");

  const customSkills: CustomSkill[] = data?.custom_skills ?? [];
  const cliPlugins: CliPlugin[] = data?.cli_plugins ?? [];
  const skillAllow = data?.agent_cli_skills ?? null;     // null = all, [] = none, [...] = allowlist
  const pluginAllow = data?.agent_cli_plugins ?? null;

  // Resolve which skills/plugins this agent actually has active.
  const activeSkills = skillAllow === null
    ? customSkills
    : customSkills.filter((s) => skillAllow.includes(s.name));
  const activePlugins = pluginAllow === null
    ? cliPlugins
    : cliPlugins.filter((p) => pluginAllow.includes(p.key) || pluginAllow.includes(p.name));

  const q = search.toLowerCase();
  const fSkills = !q ? activeSkills : activeSkills.filter((s) =>
    s.name.toLowerCase().includes(q) || (s.description ?? "").toLowerCase().includes(q));
  const fPlugins = !q ? activePlugins : activePlugins.filter((p) =>
    p.name.toLowerCase().includes(q) || p.key.toLowerCase().includes(q) || p.source.toLowerCase().includes(q));

  const loading = data === undefined;

  return (
    <div className="space-y-4">
      {/* Honest host context banner */}
      <div
        className="rounded-xl p-3.5 flex items-start gap-3"
        style={{ backgroundColor: "var(--color-bg-surface)", border: "1px solid var(--color-border)" }}
      >
        <Server size={15} className="shrink-0 mt-0.5" style={{ color: C.textSecondary }} />
        <div className="min-w-0">
          <div className="text-sm font-medium" style={{ color: "var(--color-text-primary)" }}>
            {t("hostBannerTitle")}
          </div>
          <p className="text-xs mt-1 leading-relaxed" style={{ color: "var(--color-text-muted)" }}>
            {t("hostBannerBody", { name: agentName, runtime: agentRuntime })}{" "}
            <code className="font-mono" style={{ color: "var(--color-text-secondary)" }}>~/.mc/skills</code>.
            {" "}{t("hostBannerReadonly")}{" "}
            <Link href="/skills" className="underline" style={{ color: C.accent }}>
              {t("skillsPageLink")}
            </Link>.
          </p>
        </div>
      </div>

      {/* Search */}
      <GlassCard className="flex items-center gap-2 px-3 py-2">
        <Search size={14} className="text-[var(--color-text-muted)]" />
        <input
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder={t("searchSkillsPlugins")}
          className="flex-1 bg-transparent text-sm outline-none text-[var(--color-text-primary)]"
        />
      </GlassCard>

      {/* Custom Skills */}
      <div className="space-y-2">
        <div className="flex items-center gap-2">
          <Box size={13} style={{ color: C.accent }} />
          <h2 className="text-sm font-medium" style={{ color: "var(--color-text-primary)" }}>{t("customSkills")}</h2>
          <span className="text-xs" style={{ color: "var(--color-text-muted)" }}>{t("activeCount", { count: activeSkills.length })}</span>
        </div>
        <div className="space-y-1.5">
          {fSkills.map((s) => (
            <HostSkillRow key={s.name} name={s.name} meta={s.description} badgeColor={C.accent} />
          ))}
          {fSkills.length === 0 && (
            <div className="text-xs text-center py-5 flex items-center justify-center gap-2" style={{ color: "var(--color-text-muted)" }}>
              {loading
                ? <><Loader2 size={12} className="animate-spin" /> {t("loadingEllipsis")}</>
                : search ? t("noSkillsFound") : t("noCustomActive")}
            </div>
          )}
        </div>
      </div>

      {/* CLI Plugins */}
      <div className="space-y-2">
        <div className="flex items-center gap-2">
          <Package size={13} style={{ color: C.online }} />
          <h2 className="text-sm font-medium" style={{ color: "var(--color-text-primary)" }}>{t("cliPlugins")}</h2>
          <span className="text-xs" style={{ color: "var(--color-text-muted)" }}>{t("activeCount", { count: activePlugins.length })}</span>
        </div>
        <div className="space-y-1.5">
          {fPlugins.map((p) => (
            <HostSkillRow key={p.key} name={p.name} meta={`${p.source} · v${p.version}`} badge="ready" badgeColor={C.online} />
          ))}
          {fPlugins.length === 0 && (
            <div className="text-xs text-center py-5 flex items-center justify-center gap-2" style={{ color: "var(--color-text-muted)" }}>
              {loading
                ? <><Loader2 size={12} className="animate-spin" /> {t("loadingEllipsis")}</>
                : search ? t("noPluginsFound") : t("noCliActive")}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function SkillsTab({ agentId }: { agentId: string }) {
  const t = useTranslations("agents.detail");
  const [search, setSearch] = useState("");
  const [draftCliPlugins, setDraftCliPlugins] = useState<Set<string> | null>(null);
  const qc = useQueryClient();

  // Load agent to determine runtime
  const { data: agent } = useQuery<Agent>({
    queryKey: ["agent", agentId],
    queryFn: () => api.agents.get(agentId),
    staleTime: 30_000,
  });

  const isCliBridge = agent?.agent_runtime === "cli-bridge";

  // Per-agent skills + plugins (local ~/.mc cache, runtime-agnostic, no gateway).
  const { data: agentSkillsData } = useQuery<AgentSkillsResponse>({
    queryKey: ["agent-skills", agentId],
    queryFn: () => api.skills.agentSkills(agentId),
    staleTime: 30_000,
  });

  const setAgentSkillsMutation = useMutation({
    mutationFn: (data: { skills?: string[] | null; cli_plugins?: string[] | null; update_cli_plugins?: boolean }) =>
      api.skills.setAgentSkills(agentId, data),
    onSuccess: () => {
      setDraftCliPlugins(null);
      qc.invalidateQueries({ queryKey: ["agent-skills", agentId] });
      qc.invalidateQueries({ queryKey: ["agent", agentId] });
      qc.invalidateQueries({ queryKey: ["agents"] });
      notify.success(t("skillsSaved"));
    },
    onError: (e: Error) => notify.error(e.message),
  });

  // ── CLI Plugin state ────────────────────────────────────────────────────────
  const cliPlugins = agentSkillsData?.cli_plugins ?? [];
  const savedCliPlugins = agentSkillsData?.agent_cli_plugins;
  const savedCliSet = useMemo(() => new Set(savedCliPlugins ?? []), [savedCliPlugins]);
  const currentCliSet = draftCliPlugins ?? savedCliSet;

  const cliAdded = useMemo(() => {
    if (!draftCliPlugins) return new Set<string>();
    return new Set([...draftCliPlugins].filter((k) => !savedCliSet.has(k)));
  }, [draftCliPlugins, savedCliSet]);

  const cliRemoved = useMemo(() => {
    if (!draftCliPlugins) return new Set<string>();
    return new Set([...savedCliSet].filter((k) => !draftCliPlugins.has(k)));
  }, [draftCliPlugins, savedCliSet]);

  const cliDirty = cliAdded.size > 0 || cliRemoved.size > 0;

  const handleCliToggle = (pluginKey: string) => {
    const base = draftCliPlugins ?? new Set(savedCliPlugins ?? []);
    const next = new Set(base);
    if (next.has(pluginKey)) { next.delete(pluginKey); } else { next.add(pluginKey); }
    setDraftCliPlugins(next);
  };

  const handleCliSave = () => {
    const arr = draftCliPlugins ? [...draftCliPlugins] : [];
    setAgentSkillsMutation.mutate({
      update_cli_plugins: true,
      cli_plugins: arr.length > 0 ? arr : null,
    });
  };

  // Map CLI plugins to SkillRow-compatible format
  const cliPluginRows = cliPlugins.map((p) => ({
    key: p.key,
    name: p.name,
    description: `${p.source} — v${p.version}`,
    status: "ready" as const,
    source: p.source as "bundled" | "managed" | "workspace",
  } as OpenClawSkill));

  // Host agents (Boss, Hermes, Jarvis) read skills + plugins from the shared
  // ~/.mc cache via filesystem — no gateway, no editable per-container config.
  // Show an honest read-only view instead of the retired gateway error.
  if (agent && !isCliBridge) {
    return (
      <HostSkillsView
        agentName={agent.name}
        agentRuntime={agent.agent_runtime ?? "host"}
        data={agentSkillsData}
      />
    );
  }

  if (isCliBridge) {
    return (
      <div className="space-y-4">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <h2 className="text-sm font-medium text-[var(--color-text-primary)]">{t("cliPlugins")}</h2>
            <span className="text-xs text-[var(--color-text-muted)]">
              {t("activeOfAvailable", { active: savedCliPlugins?.length ?? 0, total: cliPlugins.length })}
            </span>
          </div>
          {cliDirty && (
            <div className="flex items-center gap-2">
              <span className="text-xs text-[var(--color-text-muted)]">
                {cliAdded.size > 0 && `+${cliAdded.size}`}
                {cliAdded.size > 0 && cliRemoved.size > 0 && " / "}
                {cliRemoved.size > 0 && `-${cliRemoved.size}`}
                {" "}{t("changes", { count: cliAdded.size + cliRemoved.size })}
              </span>
              <button
                onClick={() => setDraftCliPlugins(null)}
                className="flex items-center gap-1 text-xs px-2.5 py-1.5 rounded-lg cursor-pointer"
                style={{ color: "var(--color-text-muted)", backgroundColor: "var(--color-bg-elevated)", border: "1px solid var(--color-border)" }}
              >
                <Undo2 size={12} /> {t("discard")}
              </button>
              <button
                onClick={handleCliSave}
                disabled={setAgentSkillsMutation.isPending}
                className="flex items-center gap-1 text-xs px-3 py-1.5 rounded-lg cursor-pointer"
                style={{ backgroundColor: C.accent, color: C.onAccent }}
              >
                {setAgentSkillsMutation.isPending ? <Loader2 size={12} className="animate-spin" /> : <Save size={12} />}
                {t("save")}
              </button>
            </div>
          )}
        </div>

        {/* Search */}
        <GlassCard className="flex items-center gap-2 px-3 py-2">
          <Search size={14} className="text-[var(--color-text-muted)]" />
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder={t("searchPlugins")}
            className="flex-1 bg-transparent text-sm outline-none text-[var(--color-text-primary)]"
          />
        </GlassCard>

        {cliDirty && (
          <div
            className="text-xs p-2.5 rounded-xl flex items-center gap-2"
            style={{ backgroundColor: C.accentSubtle, border: `1px solid ${C.borderAccent}`, color: C.accent }}
          >
            <Save size={13} />
            {t("unsavedChanges")}
          </div>
        )}

        <div className="space-y-1.5">
          <AnimatePresence mode="popLayout">
            {cliPluginRows.filter((s) => !search || s.name.toLowerCase().includes(search.toLowerCase()) || s.key.toLowerCase().includes(search.toLowerCase()))
              .map((skill) => {
                const isInCurrent = currentCliSet.has(skill.key);
                const pc = cliAdded.has(skill.key) ? "add" as const : cliRemoved.has(skill.key) ? "remove" as const : undefined;
                return <SkillRow key={skill.key} skill={skill} isActive={isInCurrent} pendingChange={pc} onToggle={handleCliToggle} />;
              })}
            {cliPluginRows.length === 0 && (
              <div className="text-xs text-center py-6 text-[var(--color-text-muted)]">
                {t("noCliInCache")}
              </div>
            )}
          </AnimatePresence>
        </div>
      </div>
    );
  }

  // Unreachable: both host and cli-bridge return above. Render nothing as a
  // defensive fallback while the agent query is still loading.
  return null;
}

// ── Runtime Selection Section ─────────────────────────────────────────────
// cli-bridge agents switch runtimes the "normal" way (container restart).
// Host agents with a HostHarnessAdapter (ADR-060/ADR-064) switch in place —
// same PATCH /agents/{id} endpoint, backend routes it to the in-place path.
// Host agents WITHOUT an adapter still show a locked badge — managed via
// launchd on the host, no MC-side runtime concept.
// Phase 30 dropped the `openclaw` runtime entirely (CHECK constraint on
// agents.agent_runtime). Color map reused from RuntimePill (defined above).

function RuntimeSelectionSection({ agent, agentId }: { agent: Agent; agentId: string }) {
  const t = useTranslations("agents.detail");
  const qc = useQueryClient();
  // Backend-derived (Agent.runtime_switchable). Never re-derive from harness:
  // the old `harness === "hermes"` compare locked grok/kimi/claude host agents
  // out of the picker for weeks after the backend learned to switch them.
  const isSwitchable = agent.runtime_switchable;
  // Host-inplace only steers UI details (no harness selector, in-place copy) —
  // derived from the backend verdict, not from a harness allowlist.
  const isHostInplace = agent.agent_runtime === "host" && isSwitchable;

  const { data: runtimesData } = useQuery({
    queryKey: ["runtimes"],
    queryFn: () => api.runtimes.list(),
    enabled: isSwitchable,
  });

  const [selected, setSelected] = useState<string | null>(agent.runtime_id ?? null);
  const [modalOpen, setModalOpen] = useState(false);
  const dirty = selected !== (agent.runtime_id ?? null);

  const selectedRuntime = runtimesData?.runtimes.find((r) => r.id === selected || r.slug === selected);
  const borderColor = isSwitchable && selectedRuntime
    ? RUNTIME_TYPE_COLOR[selectedRuntime.runtime_type] ?? "var(--color-border)"
    : "var(--color-border)";

  if (!isSwitchable) {
    // Locked badge for agents the backend refuses to switch. The text is the
    // backend's own reason (host_harness_adapter.runtime_switch_availability),
    // never a hardcoded sentence — the previous literal named a model
    // ("Boss = Opus 4.7") that had long since rotted.
    const reason =
      agent.runtime_switch_blocked_reason ?? t("runtimeSwitchUnsupported");
    return (
      <div
        className="rounded-xl p-4"
        style={{
          backgroundColor: "var(--color-bg-surface)",
          border: "1px solid var(--color-border)",
        }}
      >
        <div className="flex items-center gap-2 mb-2">
          <span className="text-xs font-mono text-[var(--color-text-muted)]">{t("runtimeLabel")}</span>
          <span
            className="text-[9px] px-1.5 py-0.5 rounded font-mono uppercase tracking-wide"
            style={{
              backgroundColor: "var(--color-bg-elevated)",
              color: C.textSecondary,
              border: "1px solid var(--color-border)",
            }}
          >
            locked · {agent.agent_runtime}
          </span>
        </div>
        <div className="text-[11px] text-[var(--color-text-muted)]">{reason}</div>
      </div>
    );
  }

  return (
    <>
      <div
        className="rounded-xl p-4"
        style={{
          backgroundColor: "var(--color-bg-surface)",
          border: `1px solid ${borderColor}`,
          borderLeft: `3px solid ${borderColor}`,
        }}
      >
        <div className="flex items-start justify-between gap-4">
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2 mb-2">
              <span className="text-xs font-mono text-[var(--color-text-muted)]">{t("runtimeLabel")}</span>
              {selectedRuntime?.state === "ready" && (
                <span className="w-1.5 h-1.5 rounded-full" style={{ backgroundColor: C.online }} />
              )}
              {selectedRuntime?.state && selectedRuntime.state !== "ready" && (
                <span className="text-[9px] font-mono uppercase text-[var(--color-text-muted)]">
                  {selectedRuntime.state}
                </span>
              )}
            </div>
            <select
              value={selected ?? ""}
              onChange={(e) => setSelected(e.target.value === "" ? null : e.target.value)}
              className="w-full text-sm rounded-lg px-3 py-2 outline-none cursor-pointer"
              style={{
                backgroundColor: "var(--color-bg-deep)",
                border: `1px solid ${dirty ? C.borderAccent : "var(--color-border)"}`,
                color: "var(--color-text-primary)",
              }}
            >
              <option value="">{t("fallbackOption")}</option>
              {/* Grouped by vendor via <optgroup>: the API already returns the
                  rows in provider order, this only makes that visible. The
                  label comes from the server (`provider_label`) — deriving it
                  here would be a second copy of a backend rule. Rows without a
                  recognised vendor (local vLLM, LM Studio) keep their flat
                  position after the grouped ones. */}
              {groupRuntimesByProvider(runtimesData?.runtimes ?? []).map(
                ({ label, runtimes }) => {
                  const options = runtimes.map((r) => (
                    <option key={r.id} value={r.id} disabled={!r.enabled}>
                      {r.display_name} · {r.runtime_type}
                      {r.model_identifier ? ` · ${r.model_identifier}` : ""}
                      {r.enabled ? "" : ` · ${t("runtimeDisabled")}`}
                    </option>
                  ));
                  return label ? (
                    <optgroup key={label} label={label}>
                      {options}
                    </optgroup>
                  ) : (
                    <Fragment key="__ungrouped">{options}</Fragment>
                  );
                },
              )}
            </select>
            <div className="text-[10px] text-[var(--color-text-muted)] mt-1.5">
              {isHostInplace ? (
                <>{t("inplaceHint")}</>
              ) : (
                <>
                  {t("dockerHintBefore")} <code className="font-mono">docker restart</code>{" "}
                  {t("dockerHintAfter")}
                </>
              )}
            </div>
          </div>
          <div className="pt-[22px]">
            <button
              onClick={() => {
                if (!dirty) return;
                setModalOpen(true);
              }}
              disabled={!dirty}
              className={cn(
                "flex items-center gap-1.5 text-xs px-3 py-2 rounded-lg whitespace-nowrap transition-all",
                !dirty ? "cursor-not-allowed opacity-40" : "cursor-pointer",
              )}
              style={{ backgroundColor: C.accent, color: C.onAccent }}
            >
              <RotateCcw size={12} />
              {t("switchButton")}
            </button>
          </div>
        </div>
      </div>

      {/* Phase 15 T3.1 — confirm modal with dry-run preview + force toggle */}
      <RuntimeSwitchModal
        open={modalOpen}
        onClose={() => setModalOpen(false)}
        agent={agent}
        targetRuntimeId={selected}
        onConfirm={async ({ force_when_in_progress, harness }) => {
          const res = await api.agents.switchRuntime(agentId, selected, {
            force_when_in_progress,
            harness,
          });
          qc.invalidateQueries({ queryKey: ["agent", agentId] });
          qc.invalidateQueries({ queryKey: ["agents"] });
          qc.invalidateQueries({ queryKey: ["runtimes"] });
          qc.invalidateQueries({ queryKey: ["runtime-switch-preview", agentId] });
          notify.success(
            res._switch?.image_switched
              ? t("switchedRebuilt", { s: Math.round((res._switch?.duration_ms ?? 0) / 1000) })
              : t("switched"),
          );
          return res._switch ?? null;
        }}
      />
    </>
  );
}

// ── Config Tab ───────────────────────────────────────────────────────────────

function ConfigTab({
  agentId,
  agent,
  config,
  syncConfigMutation,
}: {
  agentId: string;
  agent: Agent;
  config: Record<string, string | null> | undefined;
  syncConfigMutation: ReturnType<typeof useMutation<unknown, Error>>;
}) {
  const t = useTranslations("agents.detail");
  const [activeFile, setActiveFile] = useState("tools_md");
  const [editedContent, setEditedContent] = useState("");
  const [isDirty, setIsDirty] = useState(false);
  const qc = useQueryClient();

  // ── API Key Selector (per-agent override) ────────────────────────────────
  // Loads all secrets (masked) from the secrets table → dropdown.
  // Change via PATCH /agents/{id} { secret_id }, apply via sync-config?restart=true.
  const { data: secrets } = useQuery({
    queryKey: ["secrets"],
    queryFn: () => api.secrets.list(),
  });
  const [selectedSecretId, setSelectedSecretId] = useState<string | null>(agent.secret_id ?? null);
  const secretDirty = selectedSecretId !== (agent.secret_id ?? null);

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

  const handleSecretChange = (newValue: string) => {
    setSelectedSecretId(newValue === "" ? null : newValue);
  };

  const handleSaveSecret = async () => {
    await updateSecretMutation.mutateAsync(selectedSecretId);
  };

  const handleSaveAndApply = async () => {
    await updateSecretMutation.mutateAsync(selectedSecretId);
    await applyRestartMutation.mutateAsync();
  };

  const saveConfigMutation = useMutation({
    mutationFn: ({ fileType, content }: { fileType: string; content: string }) =>
      api.agents.config.update(agentId, fileType, content),
    onSuccess: (result) => {
      qc.invalidateQueries({ queryKey: ["agent-config"] });
      setIsDirty(false);
      if (result.warnings.length > 0) {
        result.warnings.forEach((w) => notify.warning(w));
      } else {
        notify.success(result.gateway_sync ? t("fileSavedSynced", { file: activeFile }) : t("fileSaved", { file: activeFile }));
      }
    },
    onError: () => notify.error(t("configSaveFailed")),
  });

  const handleFileChange = (fileKey: string) => {
    setActiveFile(fileKey);
    setEditedContent(config?.[fileKey] ?? "");
    setIsDirty(false);
  };

  const handleSave = () => {
    saveConfigMutation.mutate({ fileType: activeFile, content: editedContent });
  };

  const activeFileConfig = CONFIG_FILES.find((f) => f.key === activeFile);
  const isReadonly = activeFileConfig?.readonly ?? false;

  return (
    <div className="flex flex-col gap-4">
      {/* Runtime Selection ────────────────────────────────────────────── */}
      <RuntimeSelectionSection agent={agent} agentId={agentId} />

      {/* API Key Selector ─────────────────────────────────────────────── */}
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
              <span className="text-xs font-mono text-[var(--color-text-muted)]">
                API KEY (Provider)
              </span>
            </div>
            <select
              value={selectedSecretId ?? ""}
              onChange={(e) => handleSecretChange(e.target.value)}
              className="w-full text-sm rounded-lg px-3 py-2 outline-none cursor-pointer"
              style={{
                backgroundColor: "var(--color-bg-elevated)",
                border: `1px solid ${secretDirty ? C.borderAccent : "var(--color-border)"}`,
                color: "var(--color-text-primary)",
              }}
            >
              <option value="">— Fallback (docker-compose env) —</option>
              {secrets?.map((s) => (
                <option key={s.key} value={s.id}>
                  {s.label ?? s.key} {s.provider ? `· ${s.provider}` : ""}
                </option>
              ))}
            </select>
            <div className="text-[10px] text-[var(--color-text-muted)] mt-1.5">
              {t("apiKeyHint")}
            </div>
          </div>
          <div className="flex flex-col gap-2 pt-[22px]">
            <button
              onClick={handleSaveSecret}
              disabled={!secretDirty || updateSecretMutation.isPending}
              className={cn(
                "text-xs px-3 py-2 rounded-lg whitespace-nowrap transition-all",
                !secretDirty || updateSecretMutation.isPending
                  ? "cursor-not-allowed opacity-40"
                  : "cursor-pointer"
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

      {/* File editor ─────────────────────────────────────────────────── */}
      <div className="flex gap-4 min-h-[400px]">
      {/* File list */}
      <div className="flex flex-col gap-1 shrink-0 w-36">
        {CONFIG_FILES.map(({ key, label }) => (
          <button
            key={key}
            onClick={() => handleFileChange(key)}
            className={cn(
              "text-left text-[12px] font-mono px-3 py-2 rounded-lg cursor-pointer transition-all border border-transparent",
              activeFile === key
                ? "bg-[var(--color-accent-subtle)] text-[var(--color-text-primary)] border border-[var(--color-border-accent)]"
                : "text-[var(--color-text-muted)] hover:text-[var(--color-text-secondary)] hover:bg-[var(--color-bg-hover)]"
            )}
          >
            {label}
          </button>
        ))}
      </div>

      {/* Editor */}
      <div className="flex-1 flex flex-col gap-2">
        <div className="flex items-center justify-between">
          <span className="text-xs font-mono text-[var(--color-text-muted)]">
            {activeFileConfig?.label}
            {isReadonly && (
              <span
                className="ml-2 px-1.5 py-0.5 rounded text-[10px]"
                style={{ backgroundColor: "var(--color-bg-elevated)", color: "var(--color-text-muted)", border: "1px solid var(--color-border)" }}
              >
                {t("readonly")}
              </span>
            )}
          </span>
          {isDirty && !isReadonly && (
            <button
              onClick={handleSave}
              disabled={saveConfigMutation.isPending}
              className="flex items-center gap-1.5 text-xs px-3 py-1 rounded-lg cursor-pointer"
              style={{ backgroundColor: C.accent, color: C.onAccent }}
            >
              {saveConfigMutation.isPending ? <Loader2 size={12} className="animate-spin" /> : <Save size={12} />}
              {t("saveSync")}
            </button>
          )}
        </div>

        {saveConfigMutation.data?.warnings?.map((w, i) => (
          <div
            key={i}
            className="flex items-start gap-2 text-xs p-2 rounded-lg"
            style={{ backgroundColor: `${C.warning}1A`, color: C.warning, border: `1px solid ${C.warning}40` }}
          >
            <AlertTriangle size={12} className="shrink-0 mt-0.5" />
            {w}
          </div>
        ))}

        <textarea
          value={isDirty && !isReadonly ? editedContent : (config?.[activeFile] ?? "")}
          onChange={isReadonly ? undefined : (e) => { setEditedContent(e.target.value); setIsDirty(true); }}
          onFocus={isReadonly ? undefined : () => { if (!isDirty) setEditedContent(config?.[activeFile] ?? ""); }}
          readOnly={isReadonly}
          className="flex-1 w-full rounded-xl p-4 text-sm outline-none resize-none min-h-80"
          style={{
            backgroundColor: "var(--color-bg-surface)",
            border: `1px solid ${isDirty && !isReadonly ? C.borderAccent : "var(--color-border)"}`,
            color: "var(--color-text-primary)",
            fontFamily: "var(--font-mono)",
            fontSize: "13px",
            lineHeight: "1.6",
            opacity: isReadonly ? 0.7 : 1,
            cursor: isReadonly ? "default" : "text",
          }}
          placeholder={t("filePlaceholder", { file: activeFileConfig?.label ?? "" })}
          spellCheck={false}
        />

        {isReadonly && (
          <div className="flex items-center justify-between mt-1">
            <span className="text-xs text-[var(--color-text-muted)]">
              {t("autoGenerated")}
            </span>
            <button
              onClick={() => (syncConfigMutation as { mutate: () => void }).mutate()}
              disabled={syncConfigMutation.isPending}
              className="text-xs px-2 py-1 rounded-lg cursor-pointer"
              style={{ color: "var(--color-text-secondary)", backgroundColor: "var(--color-bg-elevated)", border: "1px solid var(--color-border)" }}
            >
              {syncConfigMutation.isPending ? "..." : t("regenerate")}
            </button>
          </div>
        )}
      </div>
      </div>
    </div>
  );
}

// ── Memory Tab ───────────────────────────────────────────────────────────────

function MemoryTab({ agentId, agentName }: { agentId: string; agentName: string }) {
  const t = useTranslations("agents.detail");
  const [isEditing, setIsEditing] = useState(false);
  const [editContent, setEditContent] = useState("");
  const [confirmClear, setConfirmClear] = useState(false);
  const qc = useQueryClient();

  const { data: config, isLoading } = useQuery({
    queryKey: ["agent-config", agentId],
    queryFn: () => api.agents.config.all(agentId),
  });

  const memory = config?.memory_md ?? null;

  const saveMutation = useMutation({
    mutationFn: (content: string) => api.agents.config.update(agentId, "memory_md", content),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["agent-config", agentId] });
      setIsEditing(false);
      notify.success(t("memorySaved"));
    },
    onError: () => notify.error(t("memorySaveFailed")),
  });

  const clearMutation = useMutation({
    mutationFn: () => api.agents.config.update(agentId, "memory_md", ""),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["agent-config", agentId] });
      notify.success(t("memoryCleared"));
    },
    onSettled: () => setConfirmClear(false),
  });

  if (isLoading) {
    return (
      <div className="flex justify-center py-12">
        <Loader2 size={20} className="animate-spin text-[var(--color-text-muted)]" />
      </div>
    );
  }

  if (isEditing) {
    return (
      <GlassCard className="flex flex-col min-h-[400px]">
        <div className="flex items-center justify-between p-4 border-b border-[var(--color-border)]">
          <span className="text-sm font-medium text-[var(--color-text-primary)]">
            {t("editMemory")}
          </span>
          <div className="flex gap-2">
            <button
              onClick={() => setIsEditing(false)}
              className="px-3 py-1.5 rounded-lg text-xs cursor-pointer"
              style={{ color: "var(--color-text-muted)", backgroundColor: "var(--color-bg-elevated)" }}
            >
              {t("cancel")}
            </button>
            <button
              onClick={() => saveMutation.mutate(editContent)}
              disabled={saveMutation.isPending}
              className="px-3 py-1.5 rounded-lg text-xs font-medium cursor-pointer"
              style={{ backgroundColor: C.accent, color: C.onAccent }}
            >
              {saveMutation.isPending ? t("savingDots") : t("saveSync")}
            </button>
          </div>
        </div>
        <textarea
          value={editContent}
          onChange={(e) => setEditContent(e.target.value)}
          className="flex-1 p-4 font-mono text-sm resize-none outline-none bg-transparent text-[var(--color-text-primary)]"
          style={{ minHeight: "400px" }}
          placeholder={`# ${agentName} Memory\n\n## Lessons from tasks\n- ...\n\n## Known conventions\n- ...`}
        />
      </GlassCard>
    );
  }

  return (
    <>
    <GlassCard className="flex flex-col">
      <div className="flex items-center justify-between p-4 border-b border-[var(--color-border)]">
        <span className="text-sm font-medium text-[var(--color-text-primary)]">
          {t("personalKnowledge")}
        </span>
        <div className="flex gap-2">
          {memory && (
            <button
              onClick={() => setConfirmClear(true)}
              className="px-3 py-1.5 rounded-lg text-xs cursor-pointer"
              style={{ color: C.error, backgroundColor: `${C.error}14` }}
            >
              {t("delete")}
            </button>
          )}
          <button
            onClick={() => { setEditContent(memory ?? ""); setIsEditing(true); }}
            className="px-3 py-1.5 rounded-lg text-xs font-medium cursor-pointer"
            style={{ backgroundColor: "var(--color-bg-elevated)", color: "var(--color-text-primary)", border: "1px solid var(--color-border)" }}
          >
            {t("edit")}
          </button>
        </div>
      </div>

      <div className="flex-1 overflow-auto p-6" tabIndex={0} role="region" aria-label={t("memoryAria")}>
        {memory ? (
          <div className="prose prose-invert max-w-none text-sm" style={{ color: "var(--color-text-primary)" }}>
            <ReactMarkdown>{memory}</ReactMarkdown>
          </div>
        ) : (
          <div className="flex flex-col items-center justify-center py-12 gap-3">
            <Brain size={36} className="text-[var(--color-text-muted)] opacity-30" />
            <p className="text-sm text-[var(--color-text-muted)]">
              {t("noInsights", { name: agentName })}
            </p>
            <p className="text-xs text-center max-w-xs text-[var(--color-text-muted)]">
              {t("memoryUpdateHint")}{" "}
              <code className="px-1 rounded" style={{ backgroundColor: "var(--color-bg-elevated)" }}>
                PATCH /api/v1/agent/me/memory
              </code>
            </p>
            <button
              onClick={() => { setEditContent(""); setIsEditing(true); }}
              className="mt-2 px-3 py-1.5 rounded-lg text-xs cursor-pointer"
              style={{ backgroundColor: "var(--color-bg-elevated)", color: "var(--color-text-secondary)", border: "1px solid var(--color-border)" }}
            >
              {t("fillManually")}
            </button>
          </div>
        )}
      </div>
    </GlassCard>
    <ConfirmDialog
      open={confirmClear}
      kicker={t("memoryKicker")}
      title={t("memoryClearTitle")}
      confirmLabel={t("delete")}
      loading={clearMutation.isPending}
      onConfirm={() => clearMutation.mutate()}
      onCancel={() => setConfirmClear(false)}
    />
    </>
  );
}

// ── MCP Tab ───────────────────────────────────────────────────────────────────

function AgentMcpTab({ agent }: { agent: Agent }) {
  const t = useTranslations("agents.detail");
  const { data: servers, isLoading } = useQuery({
    queryKey: ["mcp-servers"],
    queryFn: () => api.mcpServers.list(),
    staleTime: 30_000,
  });

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-12">
        <Loader2 className="animate-spin" size={20} style={{ color: "var(--color-text-muted)" }} />
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <p className="text-xs" style={{ color: "var(--color-text-muted)" }}>
        {t("mcpHint", { name: agent.name })}
      </p>
      <MCPServerMatrix servers={servers ?? []} agents={[agent]} />
    </div>
  );
}

// ── Local Memory Tab ─────────────────────────────────────────────────────────
//
// Shows the .md files in the agent container under
// /home/agent/.claude/projects/-home-agent/memory/team/.
// Use case: delete toxic lessons that the operator would otherwise only
// reach via `docker exec rm` (Sparky 2026-05-12: mc-comment-python3.md
// pushed him toward python3 urllib instead of the mc CLI).

function LocalMemoryTab({ agentId, agentName }: { agentId: string; agentName: string }) {
  const t = useTranslations("agents.detail");
  const qc = useQueryClient();
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [confirmDeleteFile, setConfirmDeleteFile] = useState<string | null>(null);

  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["agent-local-memory", agentId],
    queryFn: () => api.agents.localMemory.list(agentId),
    refetchOnWindowFocus: false,
  });

  const deleteMutation = useMutation({
    mutationFn: (filename: string) => api.agents.localMemory.delete(agentId, filename),
    onSuccess: (_, filename) => {
      notify.success(t("fileDeleted", { file: filename }));
      qc.invalidateQueries({ queryKey: ["agent-local-memory", agentId] });
    },
    onError: (err: unknown) => {
      const msg = err instanceof Error ? err.message : t("deleteFailed");
      notify.error(msg);
    },
    onSettled: () => setConfirmDeleteFile(null),
  });

  if (isLoading) {
    return (
      <div className="flex justify-center py-12">
        <Loader2 size={20} className="animate-spin" style={{ color: "var(--color-text-muted)" }} />
      </div>
    );
  }

  if (isError) {
    return (
      <GlassCard className="p-6">
        <div className="flex items-start gap-3" style={{ color: "var(--color-text-secondary)" }}>
          <AlertTriangle size={16} style={{ color: C.error }} />
          <div>
            <p className="text-sm font-medium" style={{ color: "var(--color-text-primary)" }}>
              {t("loadFailed")}
            </p>
            <p className="text-xs mt-1">{error instanceof Error ? error.message : String(error)}</p>
          </div>
        </div>
      </GlassCard>
    );
  }

  const containerState = data?.container_state;
  const files = data?.files ?? [];
  const isRunning = containerState === "running";

  return (
    <div className="space-y-4">
      <div className="flex items-start justify-between gap-4">
        <div>
          <p className="text-xs font-mono" style={{ color: "var(--color-text-muted)" }}>
            {data?.directory ?? "—"}
          </p>
          <p className="text-xs mt-1" style={{ color: "var(--color-text-secondary)" }}>
            {t("localMemoryHint", { name: agentName })}
          </p>
        </div>
        <button
          onClick={() => qc.invalidateQueries({ queryKey: ["agent-local-memory", agentId] })}
          className="p-1.5 rounded-lg cursor-pointer transition-colors"
          style={{ background: "var(--color-bg-elevated)", color: "var(--color-text-muted)" }}
          title={t("reload")}
        >
          <RefreshCw size={14} />
        </button>
      </div>

      {!isRunning && (
        <GlassCard className="p-4">
          <div className="flex items-center gap-2 text-xs" style={{ color: "var(--color-text-secondary)" }}>
            <WifiOff size={14} />
            {t("containerNotRunning", { state: containerState ?? "unknown" })}
          </div>
        </GlassCard>
      )}

      {isRunning && files.length === 0 && (
        <GlassCard className="p-6">
          <div className="text-center text-xs" style={{ color: "var(--color-text-muted)" }}>
            {t("noMdFiles")}
          </div>
        </GlassCard>
      )}

      {files.map((file) => {
        const isExpanded = expanded.has(file.name);
        return (
          <GlassCard key={file.name} className="overflow-hidden">
            <div className="flex items-center justify-between p-3 border-b" style={{ borderColor: "var(--color-border)" }}>
              <button
                onClick={() => {
                  const next = new Set(expanded);
                  if (next.has(file.name)) next.delete(file.name);
                  else next.add(file.name);
                  setExpanded(next);
                }}
                className="flex items-center gap-2 cursor-pointer text-left flex-1"
                style={{ color: "var(--color-text-primary)" }}
              >
                <HardDrive size={13} style={{ color: "var(--color-text-muted)" }} />
                <span className="text-sm font-mono">{file.name}</span>
                <span className="text-xs" style={{ color: "var(--color-text-muted)" }}>
                  {file.size.toLocaleString()} B
                  {file.truncated && ` ${t("truncated")}`}
                </span>
              </button>
              <button
                onClick={() => setConfirmDeleteFile(file.name)}
                disabled={deleteMutation.isPending}
                className="p-1.5 rounded-lg cursor-pointer transition-colors disabled:opacity-50"
                style={{
                  background: `${C.error}14`,
                  border: `1px solid ${C.error}33`,
                  color: C.error,
                }}
                title={t("deleteFile")}
              >
                <Trash2 size={13} />
              </button>
            </div>
            {isExpanded && (
              <pre
                className="p-3 text-xs font-mono whitespace-pre-wrap overflow-x-auto"
                style={{ color: "var(--color-text-secondary)", maxHeight: "400px", overflowY: "auto" }}
                tabIndex={0}
                role="region"
                aria-label={t("fileContentAria")}
              >
                {file.content || t("emptyFile")}
              </pre>
            )}
          </GlassCard>
        );
      })}
      <ConfirmDialog
        open={confirmDeleteFile !== null}
        kicker={t("localMemoryKicker")}
        title={t("deleteFileConfirm", { file: confirmDeleteFile ?? "" })}
        body={t("cannotUndo")}
        confirmLabel={t("delete")}
        loading={deleteMutation.isPending}
        onConfirm={() => { if (confirmDeleteFile) deleteMutation.mutate(confirmDeleteFile); }}
        onCancel={() => setConfirmDeleteFile(null)}
      />
    </div>
  );
}

// ── Overview Tab ─────────────────────────────────────────────────────────────

function OverviewTab({
  agent,
  agentId,
  config,
  setActiveTab,
}: {
  agent: Agent;
  agentId: string;
  config: Record<string, string | null> | undefined;
  setActiveTab: (tab: Tab) => void;
}) {
  const t = useTranslations("agents.detail");
  const locale = useLocale();
  const displaySkills = agent.skill_filter ?? agent.skills ?? [];

  const { data: activity } = useQuery({
    queryKey: ["agent-activity", agentId],
    queryFn: () => api.activity.list({ agent_id: agentId, limit: 15 }),
    refetchInterval: 60_000,
  });

  const { data: scheduledJobs } = useQuery({
    queryKey: ["schedule-jobs"],
    queryFn: () => api.schedule.listJobs(),
  });

  const agentJobs = (scheduledJobs ?? []).filter((j: ScheduledJob) => j.agent_id === agentId);

  // Health metrics
  const seenMins = agent.last_seen_at
    ? Math.floor((Date.now() - new Date(agent.last_seen_at).getTime()) / 60000)
    : null;
  const seenColor = seenMins === null
    ? C.textMuted
    : seenMins < 5
      ? C.online
      : seenMins <= 15
        ? C.warning
        : C.error;
  const runStateColorMap: Record<string, string> = {
    idle: C.textMuted,
    running: C.online,
    recovering: C.warning,
    blocked: C.error,
    aborted: C.error,
  };
  const rsColor = runStateColorMap[agent.run_state] ?? "var(--color-text-muted)";

  const [activeFile, setActiveFile] = useState("soul_md");
  const configContent = config?.[activeFile] ?? "";

  return (
    <div className="space-y-6">
      {/* KPI Row */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <GlassCard className="p-4">
          <span className="text-[11px] text-[var(--color-text-muted)]">{t("kpiTasksCompleted")}</span>
          <div className="text-2xl font-bold tracking-tight mt-1" style={{ color: C.online }}>
            {agent.total_tasks_completed}
          </div>
        </GlassCard>
        <GlassCard className="p-4">
          <span className="text-[11px] text-[var(--color-text-muted)]">{t("kpiCompactions")}</span>
          <div className="text-2xl font-bold tracking-tight mt-1 text-[var(--color-text-primary)]">
            {agent.total_compactions}
          </div>
        </GlassCard>
        <GlassCard className="p-4">
          <span className="text-[11px] text-[var(--color-text-muted)]">{t("kpiSessionMessages")}</span>
          <div className="text-2xl font-bold tracking-tight mt-1 text-[var(--color-text-primary)]">
            {agent.session_message_count}
          </div>
        </GlassCard>
        <GlassCard className="p-4">
          <span className="text-[11px] text-[var(--color-text-muted)]">{t("kpiRunState")}</span>
          <div className="mt-2">
            <span
              className="text-xs font-medium px-2 py-0.5 rounded-sm font-mono"
              style={{ color: rsColor, backgroundColor: `${rsColor}18` }}
            >
              {agent.run_state}
            </span>
          </div>
        </GlassCard>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Left column */}
        <div className="lg:col-span-1 space-y-4">
          {/* Health */}
          <GlassCard className="p-4 space-y-3">
            <h2 className="text-[11px] uppercase tracking-wider text-[var(--color-text-muted)] font-semibold">
              {t("health")}
            </h2>
            <div className="space-y-2.5">
              <div className="flex items-center justify-between">
                <span className="text-[11px] text-[var(--color-text-muted)]">{t("lastSeen")}</span>
                <div className="flex items-center gap-1.5">
                  <span className="w-1.5 h-1.5 rounded-full shrink-0" style={{ backgroundColor: seenColor }} />
                  <span className="text-[12px] font-mono" style={{ color: seenColor }}>
                    {seenMins !== null ? t("minsAgo", { mins: seenMins }) : t("never")}
                  </span>
                </div>
              </div>
              <div className="flex items-center justify-between">
                <span className="text-[11px] text-[var(--color-text-muted)]">{t("runtime")}</span>
                <RuntimePill agent={agent} />
              </div>
              <InfoRow label={t("agentType")} value={agent.agent_runtime ?? "manual"} />
              {agent.discord_channel_name && (
                <InfoRow label="Discord" value={`#${agent.discord_channel_name}`} />
              )}
            </div>
          </GlassCard>

          {/* Skills */}
          <GlassCard className="p-4 space-y-3">
            <div className="flex items-center justify-between">
              <h2 className="text-[11px] uppercase tracking-wider text-[var(--color-text-muted)] font-semibold">
                {t("skills")}
              </h2>
              <button
                onClick={() => setActiveTab("skills")}
                className="text-[10px] cursor-pointer"
                style={{ color: C.accent }}
              >
                {t("manage")}
              </button>
            </div>
            {displaySkills.length > 0 ? (
              <SkillBadges skills={displaySkills} />
            ) : (
              <div className="text-xs text-[var(--color-text-muted)]">
                {t("noSkillsAssigned")}{" "}
                <button onClick={() => setActiveTab("skills")} className="underline cursor-pointer" style={{ color: C.accent }}>
                  {t("addSkills")}
                </button>
              </div>
            )}
          </GlassCard>

          {/* Scopes */}
          {agent.scopes.length > 0 && (
            <GlassCard className="p-4 space-y-3">
              <h2 className="text-[11px] uppercase tracking-wider text-[var(--color-text-muted)] font-semibold">
                {t("scopes")}
              </h2>
              <div className="flex flex-wrap gap-1">
                {agent.scopes.map((scope) => (
                  <Pill key={scope} color={C.accent} size="sm">{scope}</Pill>
                ))}
              </div>
            </GlassCard>
          )}

          {/* Cron Jobs */}
          <GlassCard className="p-4 space-y-3">
            <h2 className="text-[11px] uppercase tracking-wider text-[var(--color-text-muted)] font-semibold">
              {t("cronJobs")}
            </h2>
            {agentJobs.length === 0 ? (
              <span className="text-xs text-[var(--color-text-muted)]">{t("noTriggers")}</span>
            ) : (
              <div className="space-y-1">
                {agentJobs.map((job: ScheduledJob) => {
                  const jobColor = !job.enabled
                    ? "var(--color-text-muted)"
                    : job.last_run_status === "failed"
                      ? C.error
                      : C.online;
                  return (
                    <div
                      key={job.id}
                      className="flex items-center gap-2 text-xs py-1 px-2 rounded-lg"
                      style={{ backgroundColor: "var(--color-bg-surface)" }}
                    >
                      <span className="w-1.5 h-1.5 rounded-full shrink-0" style={{ backgroundColor: jobColor }} />
                      <span className="flex-1 min-w-0 truncate text-[var(--color-text-primary)]">{job.name}</span>
                      <span className="font-mono shrink-0 text-[var(--color-text-muted)]">
                        {job.schedule_type === "interval" ? `${job.schedule_interval_hours}h` : job.schedule_time ?? job.schedule_type}
                      </span>
                      {!job.enabled && (
                        <span className="text-[10px] px-1 rounded text-[var(--color-text-muted)]" style={{ backgroundColor: "var(--color-bg-elevated)" }}>
                          {t("off")}
                        </span>
                      )}
                    </div>
                  );
                })}
              </div>
            )}
          </GlassCard>
        </div>

        {/* Right column: Config preview + Activity */}
        <div className="lg:col-span-2 space-y-4">
          {/* Config preview */}
          <GlassCard className="p-4">
            <div className="flex items-center gap-2 mb-3 overflow-x-auto">
              {CONFIG_FILES.map((file) => (
                <button
                  key={file.key}
                  onClick={() => setActiveFile(file.key)}
                  className={cn(
                    "text-[11px] px-2.5 py-1 rounded-lg transition-all cursor-pointer whitespace-nowrap border border-transparent",
                    activeFile === file.key
                      ? "bg-[var(--color-accent-subtle)] text-[var(--color-text-primary)] border border-[var(--color-border-accent)]"
                      : "text-[var(--color-text-muted)] hover:text-[var(--color-text-secondary)]"
                  )}
                >
                  {file.label}
                </button>
              ))}
            </div>
            <div
              className="text-[12px] font-mono leading-relaxed max-h-[400px] overflow-y-auto whitespace-pre-wrap rounded-xl p-4"
              tabIndex={0}
              role="region"
              aria-label={t("configFileAria")}
              style={{
                backgroundColor: "var(--color-bg-surface)",
                color: "var(--color-text-body)",
                border: "1px solid var(--color-border-subtle)",
              }}
            >
              {configContent || <span className="text-[var(--color-text-muted)]">{t("noContent")}</span>}
            </div>
          </GlassCard>

          {/* Activity Feed */}
          <GlassCard className="p-4 space-y-3">
            <h2 className="text-[11px] uppercase tracking-wider text-[var(--color-text-muted)] font-semibold">
              {t("activity")}
            </h2>
            {activity && activity.length > 0 ? (
              <div className="space-y-0.5">
                {activity.map((ev) => (
                  <div
                    key={ev.id}
                    className="flex items-start gap-2.5 py-1.5 px-2 rounded-lg transition-colors hover:bg-[var(--color-bg-hover)]"
                  >
                    <span
                      className="mt-1 w-1.5 h-1.5 rounded-full shrink-0"
                      style={{
                        backgroundColor:
                          ev.severity === "error" || ev.severity === "critical" ? C.error :
                          ev.severity === "warning" ? C.warning :
                          C.textMuted,
                      }}
                    />
                    <div className="min-w-0 flex-1">
                      <div className="text-xs truncate text-[var(--color-text-primary)]">{ev.title}</div>
                      <div className="text-[10px] mt-0.5 text-[var(--color-text-muted)]">{timeAgo(ev.created_at, locale)}</div>
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <div className="text-xs text-[var(--color-text-muted)]">{t("noEvents")}</div>
            )}
          </GlassCard>
        </div>
      </div>
    </div>
  );
}

function InfoRow({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="flex items-center justify-between">
      <span className="text-[11px] text-[var(--color-text-muted)]">{label}</span>
      <span className={cn("text-[12px] text-[var(--color-text-secondary)]", mono && "font-mono")}>
        {value}
      </span>
    </div>
  );
}

// ── Action Button ────────────────────────────────────────────────────────────

function ActionButton({
  icon: Icon,
  label,
  color,
  onClick,
  loading,
  disabled,
  title,
}: {
  icon: typeof Zap;
  label: string;
  color: string;
  onClick: () => void;
  loading?: boolean;
  disabled?: boolean;
  title?: string;
}) {
  return (
    <button
      onClick={onClick}
      disabled={loading || disabled}
      title={title}
      className="flex items-center justify-center gap-1.5 text-[11px] px-3 py-1.5 max-sm:w-full max-sm:py-3 max-sm:min-h-touch rounded-lg cursor-pointer transition-all disabled:opacity-50"
      style={{
        backgroundColor: `${color}18`,
        color,
        border: `1px solid ${color}30`,
      }}
    >
      {loading ? <Loader2 size={12} className="animate-spin" /> : <Icon size={12} />}
      {label}
    </button>
  );
}

// ── Agent Detail Page ────────────────────────────────────────────────────────

export default function AgentDetailPage() {
  const t = useTranslations("agents");
  const locale = useLocale();
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const qc = useQueryClient();
  const [activeTab, setActiveTab] = useState<Tab>("overview");
  const [confirmRecreate, setConfirmRecreate] = useState(false);
  const [confirmRestartProcess, setConfirmRestartProcess] = useState(false);

  // SSE updates
  const handleAgentEvent = useCallback(
    (event: string, data: Record<string, unknown>) => {
      const eventAgentId = data.agent_id as string | undefined;
      if (eventAgentId && eventAgentId !== id) return;
      qc.invalidateQueries({ queryKey: ["agent", id] });
      qc.invalidateQueries({ queryKey: ["agent-activity", id] });
    },
    [id, qc]
  );
  useAgentStream(handleAgentEvent);

  const { data: agent } = useQuery({
    queryKey: ["agent", id],
    queryFn: () => api.agents.get(id),
    refetchInterval: 60_000,
  });

  const { data: config } = useQuery({
    queryKey: ["agent-config", id],
    queryFn: () => api.agents.config.all(id),
  });

  const updateAgentMutation = useMutation({
    mutationFn: (data: Partial<Pick<Agent, "name" | "role" | "heartbeat_config" | "operational_mode">>) =>
      api.agents.update(id, data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["agent", id] });
      qc.invalidateQueries({ queryKey: ["agents"] });
      notify.success(t("detail.agentUpdated"));
    },
    onError: (e: Error) => notify.error(e.message),
  });

  const resetMutation = useMutation({
    mutationFn: () => api.agents.reset(id),
    onSuccess: () => {
      notify.success(t("detail.agentReset"));
      qc.invalidateQueries({ queryKey: ["agent", id] });
    },
    onError: () => notify.error(t("detail.resetFailed")),
  });

  const restartWorkerMutation = useMutation({
    mutationFn: () => api.agents.restartWorker(id),
    onSuccess: () => {
      notify.success(t("detail.workerRestarted"));
      qc.invalidateQueries({ queryKey: ["agent", id] });
    },
    onError: () => notify.error(t("detail.workerRestartFailed")),
  });

  // Task #19 (2026-08-08): host-only "Prozess neu starten" — full launchd
  // process restart with an orphan sweep first (13 orphaned host processes
  // were found live before this existed, one serving weeks of stale ENV).
  const restartHostProcessMutation = useMutation({
    mutationFn: () => api.agents.restartHostProcess(id),
    onSuccess: (result) => {
      notify.success(t("detail.restartProcessSucceeded", { killed: result.orphans_killed.length }));
      qc.invalidateQueries({ queryKey: ["agent", id] });
    },
    onError: (e: Error) => notify.error(t("detail.restartProcessFailed", { msg: e.message })),
    onSettled: () => setConfirmRestartProcess(false),
  });

  const forceRecreateMutation = useMutation({
    mutationFn: ({ force }: { force: boolean }) => api.agents.forceRecreateContainer(id, force),
    onSuccess: (result) => {
      notify.success(
        t("detail.containerRecreated", { s: result.duration_seconds, state: result.state }),
      );
      qc.invalidateQueries({ queryKey: ["agent", id] });
      qc.invalidateQueries({ queryKey: ["agent-local-memory", id] });
    },
    onError: (e: Error) => notify.error(t("detail.forceRecreateFailed", { msg: e.message })),
    onSettled: () => setConfirmRecreate(false),
  });

  const provisionMutation = useMutation<unknown, Error>({
    mutationFn: () => api.agents.provisionCli(id),
    onSuccess: () => {
      notify.success(t("detail.provisioned"));
      qc.invalidateQueries({ queryKey: ["agent", id] });
    },
    onError: (e: Error) => notify.error(t("detail.provisionFailedMsg", { msg: e.message })),
  });

  // Host-helper health: the Provision button silently failed with a generic
  // toast when scripts/cli-bridge.py wasn't running — now the button is
  // disabled with an actionable hint instead. Polled only while relevant.
  const { data: bridgeHealth } = useQuery({
    queryKey: ["cli-bridge-health"],
    queryFn: () => api.cliBridge.health(),
    enabled: agent?.agent_runtime === "cli-bridge" && agent?.provision_status === "local",
    refetchInterval: 30_000,
  });
  const bridgeDown = bridgeHealth?.reachable === false;

  // Latest provision-failure reason: emitted with actionable text but it
  // used to land only in the activity feed where a noob never looks.
  const provisionUnhealthy =
    agent?.agent_runtime === "cli-bridge" &&
    (agent?.provision_status === "local" || agent?.provision_status === "error");
  const { data: provisionFailEvents } = useQuery({
    queryKey: ["agent-provision-failed", id],
    queryFn: () =>
      api.activity.list({ agent_id: id, event_type: "agent.provision_failed", limit: 1 }),
    enabled: provisionUnhealthy,
    refetchInterval: 30_000,
  });
  const provisionFailure = provisionUnhealthy ? provisionFailEvents?.[0] : undefined;

  const syncConfigMutation = useMutation({
    mutationFn: () => api.agents.syncConfig(id),
    onSuccess: () => notify.success(t("detail.configSyncedGateway")),
    onError: (e: Error) => notify.error(t("detail.syncFailedMsg", { msg: e.message })),
  });

  const setupCoordMutation = useMutation({
    mutationFn: () => api.agents.setupCoordination(),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["agent", id] });
      qc.invalidateQueries({ queryKey: ["agent-config", id] });
      notify.success(t("detail.reconfigured"));
    },
    onError: () => notify.error(t("detail.reconfigureFailed")),
  });

  if (!agent) {
    return (
      <AppShell>
        <div className="flex items-center justify-center h-64">
          <Loader2 size={24} className="animate-spin text-[var(--color-text-muted)]" />
        </div>
      </AppShell>
    );
  }

  const isCliBridge = agent.agent_runtime === "cli-bridge";
  const pct = contextPercent(agent.context_tokens, agent.context_max);
  const barColor = contextColor(pct);
  const dotStatus = agentStatusToDot(agent.status);
  const provCfg = PROVISION_CONFIG[agent.provision_status] ?? PROVISION_CONFIG.local;

  return (
    <AppShell>
      <div className="space-y-6 max-w-5xl mx-auto">
        {/* Back */}
        <Link
          href="/agents"
          className="inline-flex items-center gap-1.5 text-sm text-[var(--color-text-muted)] hover:text-[var(--color-text-secondary)] transition-colors"
        >
          <ArrowLeft size={14} /> {t("allAgents")}
        </Link>

        {/* Agent Header */}
        <SpotlightCard>
          <GlassCard
            className="p-6"
            glow={
              agent.status === "online"
                ? `${C.online}14`
                : agent.status === "error"
                ? `${C.error}14`
                : undefined
            }
          >
            <div className="label-sys mb-3">Fleet · Agent</div>
            <div className="flex items-start gap-5">
              {/* Emoji */}
              <motion.div
                initial={{ scale: 0.8 }}
                animate={{ scale: 1 }}
                transition={{ type: "spring", stiffness: 300, damping: 20 }}
                className="text-5xl shrink-0"
              >
                <EntityIcon value={agent.emoji} size={44} />
              </motion.div>

              {/* Info */}
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-3 flex-wrap">
                  <h1 className="display text-2xl font-semibold text-[var(--color-text-primary)]">
                    {agent.name}
                  </h1>
                  {agent.role && (
                    <span className="text-sm text-[var(--color-text-secondary)]">-- {agent.role}</span>
                  )}
                  <Pill color={provCfg.color} size="sm">{t(provCfg.labelKey)}</Pill>
                  {agent.operational_mode === "paused" && (
                    <Pill color={C.warning} size="sm">{t("detail.paused")}</Pill>
                  )}
                  <div className="flex items-center gap-1.5 ml-auto">
                    <StatusDot status={dotStatus} pulse={dotStatus === "online" || dotStatus === "busy"} />
                    <span className="text-sm capitalize text-[var(--color-text-secondary)]">
                      {agent.status === "restarting" ? t("detail.restarting") : agent.status}
                    </span>
                  </div>
                </div>

                {/* Runtime pill + heartbeat interval */}
                <div className="flex items-center gap-3 mt-1 text-sm text-[var(--color-text-muted)] flex-wrap">
                  <RuntimePill agent={agent} />
                  <span className="flex items-center gap-1">
                    HB:{" "}
                    <select
                      value={agent.heartbeat_config?.interval ?? "5m"}
                      onChange={(e) =>
                        updateAgentMutation.mutate({
                          heartbeat_config: { ...agent.heartbeat_config, interval: e.target.value },
                        } as Partial<Pick<Agent, "name" | "role" | "heartbeat_config" | "operational_mode">>)
                      }
                      className="bg-transparent border-none text-sm cursor-pointer outline-none text-[var(--color-text-muted)]"
                    >
                      {HEARTBEAT_INTERVALS.map((hi) => (
                        <option key={hi.value} value={hi.value}>{hi.label}</option>
                      ))}
                    </select>
                  </span>
                  <span>{t("detail.lastSeenAgo", { ago: timeAgo(agent.last_seen_at, locale) })}</span>
                </div>

                {/* Context bar */}
                <div className="mt-4 max-w-sm">
                  <div className="flex items-center justify-between mb-1">
                    <span className="text-[10px] text-[var(--color-text-muted)]">{t("contextLabel")}</span>
                    <span className="text-[10px] text-[var(--color-text-muted)]">{pct}%</span>
                  </div>
                  <div className="h-1.5 rounded-full bg-[var(--color-bg-elevated)] overflow-hidden">
                    <motion.div
                      className="h-full rounded-full"
                      style={{ backgroundColor: barColor }}
                      initial={{ width: 0 }}
                      animate={{ width: `${Math.min(pct, 100)}%` }}
                      transition={{ duration: 0.6, ease: [0.16, 1, 0.3, 1] }}
                    />
                  </div>
                </div>
              </div>
            </div>

            {/* Why isn't this agent live? Latest provision failure, inline. */}
            {provisionFailure && (
              <div
                className="mt-4 rounded-lg px-3 py-2.5 text-[11px] leading-relaxed"
                style={{
                  backgroundColor: `${C.warning}14`,
                  border: `1px solid ${C.warning}33`,
                  color: "var(--color-text-secondary)",
                }}
              >
                <span className="font-medium" style={{ color: C.warning }}>
                  {t("detail.provisioningFailed")}
                </span>{" "}
                {provisionFailure.title}
              </div>
            )}

            {/* Actions — mobile: even 2-col grid (≥44px touch targets); desktop: flex-wrap row */}
            <div
              className="mt-5 pt-4 border-t grid grid-cols-2 gap-2 sm:flex sm:items-center sm:flex-wrap"
              style={{ borderColor: C.border }}
            >
              {isCliBridge ? (
                <ActionButton
                  icon={RotateCcw}
                  label={t("detail.restartWorker")}
                  color={C.warning}
                  onClick={() => restartWorkerMutation.mutate()}
                  loading={restartWorkerMutation.isPending}
                  title={t("detail.restartWorkerTitle")}
                />
              ) : (
                <ActionButton
                  icon={RotateCcw}
                  label={t("detail.reset")}
                  color={C.warning}
                  onClick={() => resetMutation.mutate()}
                  loading={resetMutation.isPending}
                />
              )}

              {agent.agent_runtime === "host" && (
                <ActionButton
                  icon={RefreshCw}
                  label={t("detail.restartProcess")}
                  color={C.error}
                  onClick={() => setConfirmRestartProcess(true)}
                  loading={restartHostProcessMutation.isPending}
                  title={t("detail.restartProcessTitle")}
                />
              )}

              {isCliBridge && (
                <ActionButton
                  icon={RefreshCw}
                  label={t("detail.forceRecreate")}
                  color={C.error}
                  onClick={() => setConfirmRecreate(true)}
                  loading={forceRecreateMutation.isPending}
                  title={t("detail.forceRecreateTitle")}
                />
              )}

              {/* Pause / Resume */}
              <ActionButton
                icon={agent.operational_mode === "paused" ? Play : Pause}
                label={agent.operational_mode === "paused" ? t("detail.resume") : t("detail.pause")}
                color={agent.operational_mode === "paused" ? C.online : C.warning}
                onClick={() => {
                  const newMode = agent.operational_mode === "paused" ? "active" : "paused";
                  updateAgentMutation.mutate({ operational_mode: newMode } as Partial<Pick<Agent, "name" | "model" | "role" | "heartbeat_config" | "operational_mode">>);
                }}
                loading={updateAgentMutation.isPending}
                title={agent.operational_mode === "paused" ? t("detail.resumeTitle") : t("detail.pauseTitle")}
              />

              {isCliBridge && agent.provision_status === "local" && (
                <>
                  <ActionButton
                    icon={Cloud}
                    label={t("detail.provision")}
                    color={C.online}
                    onClick={() => provisionMutation.mutate()}
                    loading={provisionMutation.isPending}
                    disabled={bridgeDown}
                    title={
                      bridgeDown
                        ? t("detail.bridgeDownTitle")
                        : undefined
                    }
                  />
                  {bridgeDown && (
                    <span
                      className="flex items-center gap-1.5 text-[11px] px-3 py-1.5 rounded-lg max-sm:w-full"
                      style={{
                        backgroundColor: `${C.warning}14`,
                        border: `1px solid ${C.warning}33`,
                        color: C.warning,
                      }}
                      title={t("detail.bridgeOfflineTitle")}
                    >
                      <span
                        className="w-1.5 h-1.5 rounded-full shrink-0"
                        style={{ backgroundColor: C.warning }}
                      />
                      {t("detail.bridgeOffline")}
                    </span>
                  )}
                </>
              )}

              {isCliBridge && agent.provision_status === "provisioned" && (
                <>
                  <ActionButton
                    icon={Cloud}
                    label={t("detail.syncConfig")}
                    color={C.accent}
                    onClick={() => syncConfigMutation.mutate()}
                    loading={syncConfigMutation.isPending}
                  />
                  {agent.is_board_lead && (
                    <ActionButton
                      icon={Settings}
                      label={t("detail.reconfigure")}
                      color={C.textDim}
                      onClick={() => setupCoordMutation.mutate()}
                      loading={setupCoordMutation.isPending}
                      title={t("detail.reconfigureTitle")}
                    />
                  )}
                </>
              )}

              {/* Lifecycle: Archive → (Restore) → Delete. Delete is gated on
                  archived state (backend 409 otherwise); AgentActions surfaces
                  409/422 detail in the toast. */}
              <div className="col-span-2 max-sm:mt-1 sm:col-auto sm:ml-auto">
                <AgentActions agent={agent} onDeleted={() => router.push("/agents")} />
              </div>
            </div>
          </GlassCard>
        </SpotlightCard>

        {/* Tabs — .tab-strip: mobile horizontal scroll + edge-fade (MOBILE-SPEC M17) */}
        <div className="flex items-center gap-1 border-b tab-strip" style={{ borderColor: "var(--color-border)" }}>
          {TABS.map((tab) => (
            <button
              key={tab.key}
              onClick={(e) => {
                setActiveTab(tab.key);
                // Scroll the clicked tab into view (MOBILE-SPEC)
                e.currentTarget.scrollIntoView({ inline: "nearest", behavior: "smooth" });
              }}
              className={cn(
                "flex items-center gap-1.5 px-3.5 py-2.5 text-sm cursor-pointer transition-all relative min-h-touch",
                activeTab === tab.key
                  ? "text-[var(--color-text-primary)]"
                  : "text-[var(--color-text-muted)] hover:text-[var(--color-text-secondary)]"
              )}
            >
              <tab.icon size={14} />
              {t(tab.labelKey)}
              {activeTab === tab.key && (
                <motion.div
                  layoutId="agent-tab-indicator"
                  className="absolute bottom-0 left-0 right-0 h-px"
                  style={{ backgroundColor: C.accent }}
                  transition={{ type: "spring", stiffness: 400, damping: 30 }}
                />
              )}
            </button>
          ))}
        </div>

        {/* Tab content */}
        <AnimatePresence mode="wait">
          <motion.div
            key={activeTab}
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -8 }}
            transition={{ duration: 0.2 }}
          >
            {activeTab === "overview" && <OverviewTab agent={agent} agentId={id} config={config} setActiveTab={setActiveTab} />}
            {activeTab === "skills" && <SkillsTab agentId={id} />}
            {activeTab === "mcp" && <AgentMcpTab agent={agent} />}
            {activeTab === "config" && <ConfigTab agentId={id} agent={agent} config={config} syncConfigMutation={syncConfigMutation as ReturnType<typeof useMutation<unknown, Error>>} />}
            {activeTab === "memory" && <MemoryTab agentId={id} agentName={agent.name} />}
            {activeTab === "local-memory" && <LocalMemoryTab agentId={id} agentName={agent.name} />}
          </motion.div>
        </AnimatePresence>

        <ConfirmDialog
          open={confirmRecreate}
          kicker={t("detail.forceRecreate")}
          title={t("detail.recreateTitle", { name: agent.name })}
          body={
            <>
              <p>{t("detail.recreateBody")}</p>
              {agent.current_task_id && (
                <p className="font-medium" style={{ color: C.warning }}>
                  {t("detail.recreateWarn")}
                </p>
              )}
            </>
          }
          confirmLabel={t("detail.forceRecreate")}
          loading={forceRecreateMutation.isPending}
          onConfirm={() => forceRecreateMutation.mutate({ force: !!agent.current_task_id })}
          onCancel={() => setConfirmRecreate(false)}
        />

        <ConfirmDialog
          open={confirmRestartProcess}
          kicker={t("detail.restartProcess")}
          title={t("detail.restartProcessConfirmTitle", { name: agent.name })}
          body={<p>{t("detail.restartProcessConfirmBody")}</p>}
          confirmLabel={t("detail.restartProcess")}
          loading={restartHostProcessMutation.isPending}
          onConfirm={() => restartHostProcessMutation.mutate()}
          onCancel={() => setConfirmRestartProcess(false)}
        />
      </div>
    </AppShell>
  );
}
