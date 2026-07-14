"use client";

import { useState, useCallback, useMemo, useEffect } from "react";
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
import type {
  Agent, AgentMetrics, ActivityEvent as ActivityEventType,
  OpenClawSkill, AgentSkillsResponse,
  ScheduledJob, CustomSkill, CliPlugin,
} from "@/lib/types";
import { MCPServerMatrix } from "@/components/mcp/MCPServerMatrix";
import { AgentActions } from "@/components/agent/AgentActions";

// ── Types ─────────────────────────────────────────────────────────────────────

type Tab = "overview" | "skills" | "config" | "memory" | "local-memory" | "mcp";

const TABS: { key: Tab; label: string; icon: typeof Activity }[] = [
  { key: "overview", label: "Overview", icon: Settings },
  { key: "skills", label: "Skills", icon: Wrench },
  { key: "mcp", label: "MCP", icon: Server },
  { key: "config", label: "Config", icon: FileText },
  { key: "memory", label: "Memory", icon: Brain },
  { key: "local-memory", label: "Local Memory", icon: FolderArchive },
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

const PROVISION_CONFIG: Record<string, { label: string; color: string }> = {
  local: { label: "Local", color: C.textDim },
  provisioning: { label: "Provisioning", color: C.warning },
  provisioned: { label: "Live", color: C.online },
  error: { label: "Error", color: C.error },
};

// RuntimePill + RUNTIME_TYPE_COLOR are imported from
// @/components/shared/RuntimePill at the top of the file (Phase 15 T3.4).

// ── Skills Editor (embedded) ────────────────────────────────────────────────

const SKILL_STATUS_CONFIG: Record<string, { color: string; label: string; icon: typeof CheckCircle }> = {
  ready: { color: C.online, label: "Ready", icon: CheckCircle },
  missing_bin: { color: C.warning, label: "Binary missing", icon: AlertTriangle },
  missing_env: { color: C.warning, label: "Config missing", icon: Key },
  disabled: { color: C.textDim, label: "Disabled", icon: PowerOff },
  not_installed: { color: C.error, label: "Not installed", icon: XCircle },
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
  const qc = useQueryClient();
  const cfg = SKILL_STATUS_CONFIG[skill.status] ?? SKILL_STATUS_CONFIG.not_installed;

  const installMutation = useMutation({
    mutationFn: (installId: string) => api.skills.install(skill.key, installId),
    onSuccess: () => {
      notify.success(`Installing ${skill.name}...`);
      qc.invalidateQueries({ queryKey: ["openclaw-skills"] });
    },
    onError: (e: Error) => notify.error(e.message),
  });

  const toggleMutation = useMutation({
    mutationFn: (enabled: boolean) => api.skills.update(skill.key, { enabled }),
    onSuccess: (_, enabled) => {
      notify.success(`${skill.name} ${enabled ? "enabled" : "disabled"}`);
      qc.invalidateQueries({ queryKey: ["openclaw-skills"] });
    },
    onError: (e: Error) => notify.error(e.message),
  });

  const borderColor = pendingChange === "add"
    ? `${C.online}66`
    : pendingChange === "remove"
    ? `${C.error}66`
    : skill.status === "ready"
    ? "rgba(255,255,255,0.07)"
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
        backgroundColor: bgTint ?? "rgba(255,255,255,0.03)",
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
              {skill.emoji && <span className="mr-1">{skill.emoji}</span>}
              {skill.name}
            </span>
            <span className="text-[10px] px-1.5 py-0.5 rounded-full shrink-0" style={{ color: cfg.color, backgroundColor: `${cfg.color}18` }}>
              {cfg.label}
            </span>
            {pendingChange && (
              <span
                className="text-[10px] px-1.5 py-0.5 rounded-full shrink-0 font-medium"
                style={{
                  color: pendingChange === "add" ? C.online : C.error,
                  backgroundColor: pendingChange === "add" ? `${C.online}18` : `${C.error}18`,
                }}
              >
                {pendingChange === "add" ? "+ New" : "- Removed"}
              </span>
            )}
            {skill.source !== "bundled" && (
              <span className="text-[10px] px-1.5 py-0.5 rounded-full" style={{ color: "var(--color-text-muted)", backgroundColor: "rgba(255,255,255,0.04)" }}>
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
            style={{ color: "var(--color-text-muted)", backgroundColor: "rgba(255,255,255,0.04)" }}
            title="Disable skill"
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
            title="Enable skill"
          >
            {toggleMutation.isPending ? <Loader2 size={11} className="animate-spin" /> : <Power size={11} />}
            Enable
          </button>
        )}

        {skill.homepage && (
          <a
            href={skill.homepage}
            target="_blank"
            rel="noopener noreferrer"
            className="p-1 rounded transition-colors"
            style={{ color: "var(--color-text-muted)" }}
            title="Homepage"
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
                : pendingChange === "add" ? `${C.online}18` : "rgba(255,255,255,0.04)",
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
            {pendingChange === "remove" ? "Undo" :
             pendingChange === "add" ? "Undo" :
             isActive ? "Remove" : "Add"}
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
      style={{ backgroundColor: "rgba(255,255,255,0.03)", border: "1px solid rgba(255,255,255,0.06)" }}
    >
      <span className="w-[3px] self-stretch rounded-full shrink-0" style={{ background: badgeColor ?? C.online, minHeight: 18 }} />
      <div className="min-w-0 flex-1">
        <div className="text-sm font-medium truncate" style={{ color: "var(--color-text-primary)" }}>{name}</div>
        {meta && <div className="text-xs mt-0.5 truncate" style={{ color: "var(--color-text-muted)" }}>{meta}</div>}
      </div>
      {badge && (
        <span
          className="text-[10px] px-1.5 py-0.5 rounded-full shrink-0"
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
        style={{ backgroundColor: "rgba(255,255,255,0.02)", border: "1px solid rgba(255,255,255,0.06)" }}
      >
        <Server size={15} className="shrink-0 mt-0.5" style={{ color: C.textSecondary }} />
        <div className="min-w-0">
          <div className="text-sm font-medium" style={{ color: "var(--color-text-primary)" }}>
            Host agent — Skills via filesystem
          </div>
          <p className="text-xs mt-1 leading-relaxed" style={{ color: "var(--color-text-muted)" }}>
            {agentName} runs via launchd on the Mac ({agentRuntime}) and reads Skills + CLI plugins
            directly from the shared cache <code className="font-mono" style={{ color: "var(--color-text-secondary)" }}>~/.mc/skills</code>.
            This assignment is read-only — manage it on the{" "}
            <Link href="/skills" className="underline" style={{ color: C.accent }}>
              Skills page
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
          placeholder="Search skills & plugins..."
          className="flex-1 bg-transparent text-sm outline-none text-[var(--color-text-primary)]"
        />
      </GlassCard>

      {/* Custom Skills */}
      <div className="space-y-2">
        <div className="flex items-center gap-2">
          <Box size={13} style={{ color: C.accent }} />
          <h2 className="text-sm font-medium" style={{ color: "var(--color-text-primary)" }}>Custom Skills</h2>
          <span className="text-xs" style={{ color: "var(--color-text-muted)" }}>{activeSkills.length} active</span>
        </div>
        <div className="space-y-1.5">
          {fSkills.map((s) => (
            <HostSkillRow key={s.name} name={s.name} meta={s.description} badgeColor={C.accent} />
          ))}
          {fSkills.length === 0 && (
            <div className="text-xs text-center py-5 flex items-center justify-center gap-2" style={{ color: "var(--color-text-muted)" }}>
              {loading
                ? <><Loader2 size={12} className="animate-spin" /> Loading…</>
                : search ? "No skills found" : "No custom skills active"}
            </div>
          )}
        </div>
      </div>

      {/* CLI Plugins */}
      <div className="space-y-2">
        <div className="flex items-center gap-2">
          <Package size={13} style={{ color: C.online }} />
          <h2 className="text-sm font-medium" style={{ color: "var(--color-text-primary)" }}>CLI Plugins</h2>
          <span className="text-xs" style={{ color: "var(--color-text-muted)" }}>{activePlugins.length} active</span>
        </div>
        <div className="space-y-1.5">
          {fPlugins.map((p) => (
            <HostSkillRow key={p.key} name={p.name} meta={`${p.source} · v${p.version}`} badge="ready" badgeColor={C.online} />
          ))}
          {fPlugins.length === 0 && (
            <div className="text-xs text-center py-5 flex items-center justify-center gap-2" style={{ color: "var(--color-text-muted)" }}>
              {loading
                ? <><Loader2 size={12} className="animate-spin" /> Loading…</>
                : search ? "No plugins found" : "No CLI plugins active"}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function SkillsTab({ agentId }: { agentId: string }) {
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
      notify.success("Skills saved");
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
            <h2 className="text-sm font-medium text-[var(--color-text-primary)]">CLI Plugins</h2>
            <span className="text-xs text-[var(--color-text-muted)]">
              {savedCliPlugins?.length ?? 0} active / {cliPlugins.length} available
            </span>
          </div>
          {cliDirty && (
            <div className="flex items-center gap-2">
              <span className="text-xs text-[var(--color-text-muted)]">
                {cliAdded.size > 0 && `+${cliAdded.size}`}
                {cliAdded.size > 0 && cliRemoved.size > 0 && " / "}
                {cliRemoved.size > 0 && `-${cliRemoved.size}`}
                {" "}Change{(cliAdded.size + cliRemoved.size) !== 1 ? "s" : ""}
              </span>
              <button
                onClick={() => setDraftCliPlugins(null)}
                className="flex items-center gap-1 text-xs px-2.5 py-1.5 rounded-lg cursor-pointer"
                style={{ color: "var(--color-text-muted)", backgroundColor: "rgba(255,255,255,0.04)", border: "1px solid rgba(255,255,255,0.07)" }}
              >
                <Undo2 size={12} /> Discard
              </button>
              <button
                onClick={handleCliSave}
                disabled={setAgentSkillsMutation.isPending}
                className="flex items-center gap-1 text-xs px-3 py-1.5 rounded-lg cursor-pointer"
                style={{ backgroundColor: C.accent, color: "#fff" }}
              >
                {setAgentSkillsMutation.isPending ? <Loader2 size={12} className="animate-spin" /> : <Save size={12} />}
                Save
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
            placeholder="Search plugins..."
            className="flex-1 bg-transparent text-sm outline-none text-[var(--color-text-primary)]"
          />
        </GlassCard>

        {cliDirty && (
          <div
            className="text-xs p-2.5 rounded-xl flex items-center gap-2"
            style={{ backgroundColor: C.accentSubtle, border: `1px solid ${C.borderAccent}`, color: C.accent }}
          >
            <Save size={13} />
            Unsaved changes
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
                No CLI plugins found in cache
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
// Host agents with a HostHarnessAdapter (currently only Hermes, ADR-060)
// switch in place — same PATCH /agents/{id} endpoint, backend routes it to
// the in-place path (services/agent_runtime_switch.py::_is_host_inplace).
// Host agents WITHOUT an adapter (Boss, Jarvis) still show a locked badge —
// managed via launchd on the host, no MC-side runtime concept.
// Phase 30 dropped the `openclaw` runtime entirely (CHECK constraint on
// agents.agent_runtime). Color map reused from RuntimePill (defined above).

function RuntimeSelectionSection({ agent, agentId }: { agent: Agent; agentId: string }) {
  const qc = useQueryClient();
  // ADR-060: "hermes" is the only host harness with an adapter today. Kept as
  // a plain string compare (not the cli-bridge-facing `Harness` union) —
  // mirrors RuntimeSwitchModal's `isHostInplace`.
  const isHostInplace = agent.agent_runtime === "host" && agent.harness === "hermes";
  const isSwitchable = agent.agent_runtime === "cli-bridge" || isHostInplace;

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
    ? RUNTIME_TYPE_COLOR[selectedRuntime.runtime_type] ?? "rgba(255,255,255,0.06)"
    : "rgba(255,255,255,0.06)";

  if (!isSwitchable) {
    // Locked badge for host agents without a HostHarnessAdapter (Boss, Jarvis)
    const reason =
      agent.agent_runtime === "host"
        ? "Host agent: runtime is controlled via launchd on the Mac (Boss = Opus 4.7)."
        : "Runtime switch not supported for this agent type.";
    return (
      <div
        className="rounded-xl p-4"
        style={{
          backgroundColor: "rgba(255,255,255,0.02)",
          border: "1px solid rgba(255,255,255,0.06)",
        }}
      >
        <div className="flex items-center gap-2 mb-2">
          <span className="text-xs font-mono text-[var(--color-text-muted)]">RUNTIME</span>
          <span
            className="text-[9px] px-1.5 py-0.5 rounded font-mono uppercase tracking-wide"
            style={{
              backgroundColor: "rgba(255,255,255,0.06)",
              color: C.textSecondary,
              border: "1px solid rgba(255,255,255,0.10)",
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
          backgroundColor: "rgba(255,255,255,0.02)",
          border: `1px solid ${borderColor}`,
          borderLeft: `3px solid ${borderColor}`,
        }}
      >
        <div className="flex items-start justify-between gap-4">
          <div className="flex-1">
            <div className="flex items-center gap-2 mb-2">
              <span className="text-xs font-mono text-[var(--color-text-muted)]">RUNTIME</span>
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
                backgroundColor: "rgba(255,255,255,0.04)",
                border: `1px solid ${dirty ? C.borderAccent : "rgba(255,255,255,0.08)"}`,
                color: "var(--color-text-primary)",
              }}
            >
              <option value="">— Fallback (docker-compose env) —</option>
              {runtimesData?.runtimes.map((r) => {
                const compatHint = r.enabled ? "" : " · disabled";
                return (
                  <option key={r.id} value={r.id} disabled={!r.enabled}>
                    {r.display_name} · {r.runtime_type}
                    {r.model_identifier ? ` · ${r.model_identifier}` : ""}
                    {compatHint}
                  </option>
                );
              })}
            </select>
            <div className="text-[10px] text-[var(--color-text-muted)] mt-1.5">
              {isHostInplace ? (
                <>
                  In-place switch — no parallel container. The runtime binding is
                  rewritten and the host session restarts briefly (short session
                  restart, current work is lost).
                </>
              ) : (
                <>
                  Switching triggers <code className="font-mono">docker restart</code>{" "}
                  (~5s) — for cross-image switches, a container rebuild (~30–90s).
                  Compatibility check + warnings appear in the confirm modal.
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
              style={{ backgroundColor: C.accent, color: "white" }}
            >
              <RotateCcw size={12} />
              Switch…
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
              ? `Runtime switched — image rebuilt (${Math.round((res._switch?.duration_ms ?? 0) / 1000)}s)`
              : "Runtime switched",
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
      notify.success("API key saved");
    },
    onError: (e: Error) => notify.error(`Failed to save: ${e.message}`),
  });

  const applyRestartMutation = useMutation({
    mutationFn: () => api.agents.syncConfig(agentId, { restart: true }),
    onSuccess: (result) => {
      const restartStatus = result.restart?.status ?? "no restart";
      notify.success(`Config synced + ${restartStatus}`);
      qc.invalidateQueries({ queryKey: ["agent", agentId] });
    },
    onError: (e: Error) => notify.error(`Sync failed: ${e.message}`),
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
        notify.success(`${activeFile} saved${result.gateway_sync ? " & synced to gateway" : ""}`);
      }
    },
    onError: () => notify.error("Failed to save config"),
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
          backgroundColor: "rgba(255,255,255,0.02)",
          border: "1px solid rgba(255,255,255,0.06)",
        }}
      >
        <div className="flex items-start justify-between gap-4">
          <div className="flex-1">
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
                backgroundColor: "rgba(255,255,255,0.04)",
                border: `1px solid ${secretDirty ? C.borderAccent : "rgba(255,255,255,0.08)"}`,
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
              From Settings → API Keys. Written to the container's .env on Apply and loaded on openclaude start.
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
                backgroundColor: "rgba(255,255,255,0.04)",
                border: "1px solid rgba(255,255,255,0.08)",
                color: "var(--color-text-secondary)",
              }}
            >
              {updateSecretMutation.isPending ? "Saving…" : "Save"}
            </button>
            <button
              onClick={handleSaveAndApply}
              disabled={applyRestartMutation.isPending || updateSecretMutation.isPending}
              className="flex items-center gap-1.5 text-xs px-3 py-2 rounded-lg whitespace-nowrap cursor-pointer"
              style={{ backgroundColor: C.accent, color: "white" }}
            >
              {applyRestartMutation.isPending ? (
                <Loader2 size={12} className="animate-spin" />
              ) : (
                <RotateCcw size={12} />
              )}
              Apply & Restart
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
              "text-left text-[12px] font-mono px-3 py-2 rounded-lg cursor-pointer transition-all",
              activeFile === key
                ? "bg-[rgba(15,163,163,0.12)] text-[#0FA3A3]"
                : "text-[var(--color-text-muted)] hover:text-[var(--color-text-secondary)] hover:bg-[rgba(255,255,255,0.03)]"
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
                style={{ backgroundColor: "rgba(255,255,255,0.04)", color: "var(--color-text-muted)", border: "1px solid rgba(255,255,255,0.07)" }}
              >
                readonly
              </span>
            )}
          </span>
          {isDirty && !isReadonly && (
            <button
              onClick={handleSave}
              disabled={saveConfigMutation.isPending}
              className="flex items-center gap-1.5 text-xs px-3 py-1 rounded-lg cursor-pointer"
              style={{ backgroundColor: C.accent, color: "white" }}
            >
              {saveConfigMutation.isPending ? <Loader2 size={12} className="animate-spin" /> : <Save size={12} />}
              Save & Sync
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
            backgroundColor: "rgba(255,255,255,0.02)",
            border: `1px solid ${isDirty && !isReadonly ? C.borderAccent : "rgba(255,255,255,0.06)"}`,
            color: "var(--color-text-primary)",
            fontFamily: "var(--font-mono)",
            fontSize: "13px",
            lineHeight: "1.6",
            opacity: isReadonly ? 0.7 : 1,
            cursor: isReadonly ? "default" : "text",
          }}
          placeholder={`${activeFileConfig?.label} content...`}
          spellCheck={false}
        />

        {isReadonly && (
          <div className="flex items-center justify-between mt-1">
            <span className="text-xs text-[var(--color-text-muted)]">
              Auto-generiert -- zeigt Operator-Kontext fuer diesen Agent
            </span>
            <button
              onClick={() => (syncConfigMutation as { mutate: () => void }).mutate()}
              disabled={syncConfigMutation.isPending}
              className="text-xs px-2 py-1 rounded-lg cursor-pointer"
              style={{ color: "var(--color-text-secondary)", backgroundColor: "rgba(255,255,255,0.04)", border: "1px solid rgba(255,255,255,0.07)" }}
            >
              {syncConfigMutation.isPending ? "..." : "Regenerate"}
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
  const [isEditing, setIsEditing] = useState(false);
  const [editContent, setEditContent] = useState("");
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
      notify.success("Memory saved & pushed to gateway");
    },
    onError: () => notify.error("Failed to save"),
  });

  const clearMutation = useMutation({
    mutationFn: () => api.agents.config.update(agentId, "memory_md", ""),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["agent-config", agentId] });
      notify.success("Memory cleared");
    },
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
        <div className="flex items-center justify-between p-4 border-b border-[rgba(255,255,255,0.06)]">
          <span className="text-sm font-medium text-[var(--color-text-primary)]">
            Edit MEMORY.md
          </span>
          <div className="flex gap-2">
            <button
              onClick={() => setIsEditing(false)}
              className="px-3 py-1.5 rounded-lg text-xs cursor-pointer"
              style={{ color: "var(--color-text-muted)", backgroundColor: "rgba(255,255,255,0.04)" }}
            >
              Cancel
            </button>
            <button
              onClick={() => saveMutation.mutate(editContent)}
              disabled={saveMutation.isPending}
              className="px-3 py-1.5 rounded-lg text-xs font-medium cursor-pointer"
              style={{ backgroundColor: C.accent, color: "white" }}
            >
              {saveMutation.isPending ? "Saving..." : "Save & Sync"}
            </button>
          </div>
        </div>
        <textarea
          value={editContent}
          onChange={(e) => setEditContent(e.target.value)}
          className="flex-1 p-4 font-mono text-sm resize-none outline-none bg-transparent text-[var(--color-text-primary)]"
          style={{ minHeight: "400px" }}
          placeholder={`# ${agentName} Memory\n\n## Gelerntes aus Tasks\n- ...\n\n## Bekannte Konventionen\n- ...`}
        />
      </GlassCard>
    );
  }

  return (
    <GlassCard className="flex flex-col">
      <div className="flex items-center justify-between p-4 border-b border-[rgba(255,255,255,0.06)]">
        <span className="text-sm font-medium text-[var(--color-text-primary)]">
          Persoenliches Wissen
        </span>
        <div className="flex gap-2">
          {memory && (
            <button
              onClick={() => { if (confirm("Really delete memory?")) clearMutation.mutate(); }}
              className="px-3 py-1.5 rounded-lg text-xs cursor-pointer"
              style={{ color: C.error, backgroundColor: `${C.error}14` }}
            >
              Delete
            </button>
          )}
          <button
            onClick={() => { setEditContent(memory ?? ""); setIsEditing(true); }}
            className="px-3 py-1.5 rounded-lg text-xs font-medium cursor-pointer"
            style={{ backgroundColor: "rgba(255,255,255,0.04)", color: "var(--color-text-primary)", border: "1px solid rgba(255,255,255,0.07)" }}
          >
            Edit
          </button>
        </div>
      </div>

      <div className="flex-1 overflow-auto p-6" tabIndex={0} role="region" aria-label="Agent memory content">
        {memory ? (
          <div className="prose prose-invert max-w-none text-sm" style={{ color: "var(--color-text-primary)" }}>
            <ReactMarkdown>{memory}</ReactMarkdown>
          </div>
        ) : (
          <div className="flex flex-col items-center justify-center py-12 gap-3">
            <Brain size={36} className="text-[var(--color-text-muted)] opacity-30" />
            <p className="text-sm text-[var(--color-text-muted)]">
              {agentName} hasn't saved any insights yet.
            </p>
            <p className="text-xs text-center max-w-xs text-[var(--color-text-muted)]">
              Agents aktualisieren ihre Memory via{" "}
              <code className="px-1 rounded" style={{ backgroundColor: "rgba(255,255,255,0.04)" }}>
                PATCH /api/v1/agent/me/memory
              </code>
            </p>
            <button
              onClick={() => { setEditContent(""); setIsEditing(true); }}
              className="mt-2 px-3 py-1.5 rounded-lg text-xs cursor-pointer"
              style={{ backgroundColor: "rgba(255,255,255,0.04)", color: "var(--color-text-secondary)", border: "1px solid rgba(255,255,255,0.07)" }}
            >
              Fill in manually
            </button>
          </div>
        )}
      </div>
    </GlassCard>
  );
}

// ── MCP Tab ───────────────────────────────────────────────────────────────────

function AgentMcpTab({ agent }: { agent: Agent }) {
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
        MCP server assignment for {agent.name}. Disable servers this agent doesn't need.
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
  const qc = useQueryClient();
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["agent-local-memory", agentId],
    queryFn: () => api.agents.localMemory.list(agentId),
    refetchOnWindowFocus: false,
  });

  const deleteMutation = useMutation({
    mutationFn: (filename: string) => api.agents.localMemory.delete(agentId, filename),
    onSuccess: (_, filename) => {
      notify.success(`${filename} deleted`);
      qc.invalidateQueries({ queryKey: ["agent-local-memory", agentId] });
    },
    onError: (err: unknown) => {
      const msg = err instanceof Error ? err.message : "Delete failed";
      notify.error(msg);
    },
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
              Failed to load
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
            Claude local memory files for {agentName}. Read by the agent on every
            turn — wrong lessons here permanently distort behavior.
          </p>
        </div>
        <button
          onClick={() => qc.invalidateQueries({ queryKey: ["agent-local-memory", agentId] })}
          className="p-1.5 rounded-lg cursor-pointer transition-colors"
          style={{ background: "rgba(255,255,255,0.04)", color: "var(--color-text-muted)" }}
          title="Reload"
        >
          <RefreshCw size={14} />
        </button>
      </div>

      {!isRunning && (
        <GlassCard className="p-4">
          <div className="flex items-center gap-2 text-xs" style={{ color: "var(--color-text-secondary)" }}>
            <WifiOff size={14} />
            Container not running (state: {containerState ?? "unknown"}). Start the container
            to see the files.
          </div>
        </GlassCard>
      )}

      {isRunning && files.length === 0 && (
        <GlassCard className="p-6">
          <div className="text-center text-xs" style={{ color: "var(--color-text-muted)" }}>
            No .md files in the memory directory.
          </div>
        </GlassCard>
      )}

      {files.map((file) => {
        const isExpanded = expanded.has(file.name);
        return (
          <GlassCard key={file.name} className="overflow-hidden">
            <div className="flex items-center justify-between p-3 border-b" style={{ borderColor: "rgba(255,255,255,0.06)" }}>
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
                  {file.truncated && " (truncated)"}
                </span>
              </button>
              <button
                onClick={() => {
                  if (confirm(`Really delete "${file.name}"? This action cannot be undone.`)) {
                    deleteMutation.mutate(file.name);
                  }
                }}
                disabled={deleteMutation.isPending}
                className="p-1.5 rounded-lg cursor-pointer transition-colors disabled:opacity-50"
                style={{
                  background: `${C.error}14`,
                  border: `1px solid ${C.error}33`,
                  color: C.error,
                }}
                title="Delete file"
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
                aria-label="File content"
              >
                {file.content || "(empty)"}
              </pre>
            )}
          </GlassCard>
        );
      })}
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
          <span className="text-[11px] text-[var(--color-text-muted)]">Tasks Completed</span>
          <div className="text-2xl font-bold tracking-tight mt-1" style={{ color: C.online }}>
            {agent.total_tasks_completed}
          </div>
        </GlassCard>
        <GlassCard className="p-4">
          <span className="text-[11px] text-[var(--color-text-muted)]">Compactions</span>
          <div className="text-2xl font-bold tracking-tight mt-1 text-[var(--color-text-primary)]">
            {agent.total_compactions}
          </div>
        </GlassCard>
        <GlassCard className="p-4">
          <span className="text-[11px] text-[var(--color-text-muted)]">Session Messages</span>
          <div className="text-2xl font-bold tracking-tight mt-1 text-[var(--color-text-primary)]">
            {agent.session_message_count}
          </div>
        </GlassCard>
        <GlassCard className="p-4">
          <span className="text-[11px] text-[var(--color-text-muted)]">Run State</span>
          <div className="mt-2">
            <span
              className="text-xs font-medium px-2 py-0.5 rounded-full"
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
              Health
            </h2>
            <div className="space-y-2.5">
              <div className="flex items-center justify-between">
                <span className="text-[11px] text-[var(--color-text-muted)]">Last Seen</span>
                <div className="flex items-center gap-1.5">
                  <span className="w-1.5 h-1.5 rounded-full shrink-0" style={{ backgroundColor: seenColor }} />
                  <span className="text-[12px] font-mono" style={{ color: seenColor }}>
                    {seenMins !== null ? `${seenMins}m ago` : "never"}
                  </span>
                </div>
              </div>
              <div className="flex items-center justify-between">
                <span className="text-[11px] text-[var(--color-text-muted)]">Runtime</span>
                <RuntimePill agent={agent} />
              </div>
              <InfoRow label="Agent Type" value={agent.agent_runtime ?? "manual"} />
              {agent.discord_channel_name && (
                <InfoRow label="Discord" value={`#${agent.discord_channel_name}`} />
              )}
            </div>
          </GlassCard>

          {/* Skills */}
          <GlassCard className="p-4 space-y-3">
            <div className="flex items-center justify-between">
              <h2 className="text-[11px] uppercase tracking-wider text-[var(--color-text-muted)] font-semibold">
                Skills
              </h2>
              <button
                onClick={() => setActiveTab("skills")}
                className="text-[10px] cursor-pointer"
                style={{ color: C.accent }}
              >
                Manage
              </button>
            </div>
            {displaySkills.length > 0 ? (
              <SkillBadges skills={displaySkills} />
            ) : (
              <div className="text-xs text-[var(--color-text-muted)]">
                No skills assigned —{" "}
                <button onClick={() => setActiveTab("skills")} className="underline cursor-pointer" style={{ color: C.accent }}>
                  Add skills
                </button>
              </div>
            )}
          </GlassCard>

          {/* Scopes */}
          {agent.scopes.length > 0 && (
            <GlassCard className="p-4 space-y-3">
              <h2 className="text-[11px] uppercase tracking-wider text-[var(--color-text-muted)] font-semibold">
                Scopes
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
              Cron Jobs
            </h2>
            {agentJobs.length === 0 ? (
              <span className="text-xs text-[var(--color-text-muted)]">No active triggers</span>
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
                      style={{ backgroundColor: "rgba(255,255,255,0.02)" }}
                    >
                      <span className="w-1.5 h-1.5 rounded-full shrink-0" style={{ backgroundColor: jobColor }} />
                      <span className="flex-1 min-w-0 truncate text-[var(--color-text-primary)]">{job.name}</span>
                      <span className="font-mono shrink-0 text-[var(--color-text-muted)]">
                        {job.schedule_type === "interval" ? `${job.schedule_interval_hours}h` : job.schedule_time ?? job.schedule_type}
                      </span>
                      {!job.enabled && (
                        <span className="text-[10px] px-1 rounded text-[var(--color-text-muted)]" style={{ backgroundColor: "rgba(255,255,255,0.04)" }}>
                          off
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
                    "text-[11px] px-2.5 py-1 rounded-lg transition-all cursor-pointer whitespace-nowrap",
                    activeFile === file.key
                      ? "bg-[rgba(15,163,163,0.15)] text-[#0FA3A3]"
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
              aria-label="Config file content"
              style={{
                backgroundColor: "rgba(255,255,255,0.02)",
                color: "var(--color-text-body)",
                border: "1px solid rgba(255,255,255,0.04)",
              }}
            >
              {configContent || <span className="text-[var(--color-text-muted)]">Kein Inhalt</span>}
            </div>
          </GlassCard>

          {/* Activity Feed */}
          <GlassCard className="p-4 space-y-3">
            <h2 className="text-[11px] uppercase tracking-wider text-[var(--color-text-muted)] font-semibold">
              Activity
            </h2>
            {activity && activity.length > 0 ? (
              <div className="space-y-0.5">
                {activity.map((ev) => (
                  <div
                    key={ev.id}
                    className="flex items-start gap-2.5 py-1.5 px-2 rounded-lg transition-colors hover:bg-[rgba(255,255,255,0.02)]"
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
                      <div className="text-[10px] mt-0.5 text-[var(--color-text-muted)]">{timeAgo(ev.created_at)}</div>
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <div className="text-xs text-[var(--color-text-muted)]">No events yet</div>
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
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const qc = useQueryClient();
  const [activeTab, setActiveTab] = useState<Tab>("overview");

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
      notify.success("Agent updated");
    },
    onError: (e: Error) => notify.error(e.message),
  });

  const resetMutation = useMutation({
    mutationFn: () => api.agents.reset(id),
    onSuccess: () => {
      notify.success("Agent reset");
      qc.invalidateQueries({ queryKey: ["agent", id] });
    },
    onError: () => notify.error("Reset failed"),
  });

  const restartWorkerMutation = useMutation({
    mutationFn: () => api.agents.restartWorker(id),
    onSuccess: () => {
      notify.success("Worker restarted");
      qc.invalidateQueries({ queryKey: ["agent", id] });
    },
    onError: () => notify.error("Worker restart failed"),
  });

  const forceRecreateMutation = useMutation({
    mutationFn: ({ force }: { force: boolean }) => api.agents.forceRecreateContainer(id, force),
    onSuccess: (result) => {
      notify.success(
        `Container recreated in ${result.duration_seconds}s (state: ${result.state})`,
      );
      qc.invalidateQueries({ queryKey: ["agent", id] });
      qc.invalidateQueries({ queryKey: ["agent-local-memory", id] });
    },
    onError: (e: Error) => notify.error(`Force recreate failed: ${e.message}`),
  });

  const provisionMutation = useMutation<unknown, Error>({
    mutationFn: () => api.agents.provisionCli(id),
    onSuccess: () => {
      notify.success("Agent provisioned");
      qc.invalidateQueries({ queryKey: ["agent", id] });
    },
    onError: (e: Error) => notify.error(`Provisioning failed: ${e.message}`),
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
    onSuccess: () => notify.success("Config synced to gateway"),
    onError: (e: Error) => notify.error(`Sync failed: ${e.message}`),
  });

  const setupCoordMutation = useMutation({
    mutationFn: () => api.agents.setupCoordination(),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["agent", id] });
      qc.invalidateQueries({ queryKey: ["agent-config", id] });
      notify.success("Agents reconfigured -- templates + USER.md + MEMORY.md pushed");
    },
    onError: () => notify.error("Failed to reconfigure"),
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
          <ArrowLeft size={14} /> All Agents
        </Link>

        {/* Agent Header */}
        <SpotlightCard>
          <GlassCard
            className="p-6"
            glow={
              agent.status === "online"
                ? "rgba(0, 204, 136, 0.08)"
                : agent.status === "error"
                ? "rgba(239, 68, 68, 0.08)"
                : undefined
            }
          >
            <div className="flex items-start gap-5">
              {/* Emoji */}
              <motion.div
                initial={{ scale: 0.8 }}
                animate={{ scale: 1 }}
                transition={{ type: "spring", stiffness: 300, damping: 20 }}
                className="text-5xl shrink-0"
              >
                {agent.emoji ?? ""}
              </motion.div>

              {/* Info */}
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-3 flex-wrap">
                  <h1 className="text-2xl font-bold tracking-tight text-[var(--color-text-primary)]">
                    {agent.name}
                  </h1>
                  {agent.role && (
                    <span className="text-sm text-[var(--color-text-secondary)]">-- {agent.role}</span>
                  )}
                  <Pill color={provCfg.color} size="sm">{provCfg.label}</Pill>
                  {agent.operational_mode === "paused" && (
                    <Pill color={C.warning} size="sm">Paused</Pill>
                  )}
                  <div className="flex items-center gap-1.5 ml-auto">
                    <StatusDot status={dotStatus} pulse={dotStatus === "online" || dotStatus === "busy"} />
                    <span className="text-sm capitalize text-[var(--color-text-secondary)]">
                      {agent.status === "restarting" ? "Restarting..." : agent.status}
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
                  <span>Last seen: {timeAgo(agent.last_seen_at)}</span>
                </div>

                {/* Context bar */}
                <div className="mt-4 max-w-sm">
                  <div className="flex items-center justify-between mb-1">
                    <span className="text-[10px] text-[var(--color-text-muted)]">Context</span>
                    <span className="text-[10px] text-[var(--color-text-muted)]">{pct}%</span>
                  </div>
                  <div className="h-1.5 rounded-full bg-[rgba(255,255,255,0.06)] overflow-hidden">
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
                  Provisioning failed:
                </span>{" "}
                {provisionFailure.title}
              </div>
            )}

            {/* Actions — mobile: even 2-col grid (≥44px touch targets); desktop: flex-wrap row */}
            <div
              className="mt-5 pt-4 border-t grid grid-cols-2 gap-2 sm:flex sm:items-center sm:flex-wrap"
              style={{ borderColor: "rgba(255,255,255,0.06)" }}
            >
              {isCliBridge ? (
                <ActionButton
                  icon={RotateCcw}
                  label="Restart Worker"
                  color={C.warning}
                  onClick={() => restartWorkerMutation.mutate()}
                  loading={restartWorkerMutation.isPending}
                  title="Restart worker session"
                />
              ) : (
                <ActionButton
                  icon={RotateCcw}
                  label="Reset"
                  color={C.warning}
                  onClick={() => resetMutation.mutate()}
                  loading={resetMutation.isPending}
                />
              )}

              {isCliBridge && (
                <ActionButton
                  icon={RefreshCw}
                  label="Force-Recreate"
                  color={C.error}
                  onClick={() => {
                    const hasTask = !!agent.current_task_id;
                    const baseMsg = `Fully recreate container ${agent.name}?\n\nThis pulls the current Docker image (~30-90s).\nThe running worker session will be terminated.`;
                    const taskWarning = hasTask
                      ? `\n\nWARNING: The agent is currently working on a task — the run will be aborted.\nClick OK to continue anyway (force=true).`
                      : "";
                    if (confirm(baseMsg + taskWarning)) {
                      forceRecreateMutation.mutate({ force: hasTask });
                    }
                  }}
                  loading={forceRecreateMutation.isPending}
                  title="Recreate container (pulls current image)"
                />
              )}

              {/* Pause / Resume */}
              <ActionButton
                icon={agent.operational_mode === "paused" ? Play : Pause}
                label={agent.operational_mode === "paused" ? "Resume" : "Pause"}
                color={agent.operational_mode === "paused" ? C.online : C.warning}
                onClick={() => {
                  const newMode = agent.operational_mode === "paused" ? "active" : "paused";
                  updateAgentMutation.mutate({ operational_mode: newMode } as Partial<Pick<Agent, "name" | "model" | "role" | "heartbeat_config" | "operational_mode">>);
                }}
                loading={updateAgentMutation.isPending}
                title={agent.operational_mode === "paused" ? "Resume agent" : "Pause agent"}
              />

              {isCliBridge && agent.provision_status === "local" && (
                <>
                  <ActionButton
                    icon={Cloud}
                    label="Provision"
                    color={C.online}
                    onClick={() => provisionMutation.mutate()}
                    loading={provisionMutation.isPending}
                    disabled={bridgeDown}
                    title={
                      bridgeDown
                        ? "cli-bridge helper not reachable — start it on the host: python3 scripts/cli-bridge.py"
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
                      title="Start the host helper, then Provision becomes available: python3 scripts/cli-bridge.py"
                    >
                      <span
                        className="w-1.5 h-1.5 rounded-full shrink-0"
                        style={{ backgroundColor: C.warning }}
                      />
                      bridge offline
                    </span>
                  )}
                </>
              )}

              {isCliBridge && agent.provision_status === "provisioned" && (
                <>
                  <ActionButton
                    icon={Cloud}
                    label="Sync Config"
                    color={C.accent}
                    onClick={() => syncConfigMutation.mutate()}
                    loading={syncConfigMutation.isPending}
                  />
                  {agent.is_board_lead && (
                    <ActionButton
                      icon={Settings}
                      label="Reconfigure"
                      color={C.textDim}
                      onClick={() => setupCoordMutation.mutate()}
                      loading={setupCoordMutation.isPending}
                      title="Regenerate templates + push to worker"
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
        <div className="flex items-center gap-1 border-b tab-strip" style={{ borderColor: "rgba(255,255,255,0.06)" }}>
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
              {tab.label}
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
      </div>
    </AppShell>
  );
}
