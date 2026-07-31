"use client";

import { useState, useMemo, useEffect } from "react";
import Link from "next/link";
import AppShell from "@/components/layout/AppShell";
import { useQuery, useQueryClient, useMutation } from "@tanstack/react-query";
import { motion, AnimatePresence } from "framer-motion";
import { useLocale, useTranslations } from "next-intl";
import {
  Plus, X, Loader2, Bot, Users, RotateCcw, Settings, BarChart3,
  Layout, ChevronDown, Archive, MoreVertical,
} from "lucide-react";
import { api } from "@/lib/api";
import { useAppStore } from "@/lib/store";
import { useAgentStream } from "@/lib/sse";
import { contextPercent, timeAgo } from "@/lib/utils";
import { notify } from "@/lib/notify";
import { AgentGrid } from "@/components/agent/AgentGrid";
import { GlassCard } from "@/components/shared/GlassCard";
import { Pill } from "@/components/shared/Pill";
import { StatusDot } from "@/components/shared/StatusDot";
import { SkillBadges } from "@/components/agent/AgentCard";
import { C } from "@/lib/colors";
import { useBodyScrollLock } from "@/hooks/useBodyScrollLock";
import type { Agent, Board } from "@/lib/types";
import { HARNESS_LABELS, type Harness } from "@/lib/types";
import { HarnessIcon, harnessLabel } from "@/components/shared/HarnessIcon";
import { AgentWizard } from "./wizard/AgentWizard";
import { AgentActions, extractDetail } from "@/components/agent/AgentActions";
import type { WizardState } from "./wizard/types";
import { EntityIcon } from "@/components/shared/EntityIcon";

// ── Design Tokens (migrated from CINEMA inline map → lib/colors.ts) ────────
const CINEMA = {
  modalBg: C.bgBase,
  border: C.border,
  borderSubtle: C.borderSubtle,
  surfaceBg: "var(--color-border-subtle)",
  errorBg: `${C.error}1F`,
  warningBg: `${C.warning}14`,
  warningBorder: `${C.warning}33`,
} as const;

const modalOverlayClass = "fixed inset-0 z-50 flex items-end sm:items-center justify-center px-3 sm:px-4";
const modalBackdropClass = "absolute inset-0 bg-black/70 backdrop-blur-sm";
const modalCardStyle = {
  backgroundColor: CINEMA.modalBg,
  border: `1px solid ${CINEMA.border}`,
  boxShadow: "0 4px 24px rgba(0,0,0,0.5), 0 1px 2px rgba(0,0,0,0.3)",
};
const inputStyle = {
  border: `1px solid ${CINEMA.border}`,
  color: "var(--color-text-primary)",
};
const inputClass = "w-full px-3 py-2.5 text-sm rounded-xl bg-transparent outline-none transition-colors";
const btnCancelClass = "px-4 py-2.5 text-sm rounded-xl cursor-pointer transition-colors text-[var(--color-text-secondary)] hover:text-[var(--color-text-primary)]";
const btnPrimaryStyle = {
  background: C.accent,
  color: C.onAccent,
};
const selectStyle = {
  ...inputStyle,
  backgroundColor: CINEMA.modalBg,
};

const ease = [0.16, 1, 0.3, 1] as const;

// ── Assign Board Modal ──────────────────────────────────────────────────────

function AssignBoardModal({
  agent,
  boards,
  onClose,
}: {
  agent: Agent;
  boards: Board[];
  onClose: () => void;
}) {
  const t = useTranslations("agents");
  const [boardId, setBoardId] = useState(agent.board_id ?? "");
  const qc = useQueryClient();

  // Panel register rule 4: scroll-lock + Esc closes (backdrop click below).
  useBodyScrollLock(true);
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [onClose]);

  async function handleAssign() {
    try {
      await api.agents.assignBoard(agent.id, boardId || null);
      notify.success(t("assignedToBoard", { name: agent.name }));
      qc.invalidateQueries({ queryKey: ["agents"] });
      onClose();
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : "Error";
      notify.error(`Error: ${msg}`);
    }
  }

  return (
    <div className={modalOverlayClass} onClick={onClose}>
      <div className={modalBackdropClass} />
      <motion.div
        initial={{ opacity: 0, scale: 0.95, y: 8 }}
        animate={{ opacity: 1, scale: 1, y: 0 }}
        exit={{ opacity: 0, scale: 0.95, y: 8 }}
        transition={{ duration: 0.2, ease }}
        className="relative w-full max-w-sm rounded-t-2xl sm:rounded-2xl overflow-hidden max-h-[90dvh] overflow-y-auto"
        style={modalCardStyle}
        onClick={(e) => e.stopPropagation()}
      >
        <div
          className="flex items-center justify-between px-5 py-4 border-b"
          style={{ borderColor: CINEMA.borderSubtle }}
        >
          <h2 className="text-sm font-semibold text-[var(--color-text-primary)]">
            <EntityIcon value={agent.emoji} size={14} className="inline-block align-[-2px] mr-1" />{agent.name} — {t("assignBoard")}
          </h2>
          <button onClick={onClose} className="cursor-pointer text-[var(--color-text-muted)]">
            <X size={16} />
          </button>
        </div>

        <div className="p-5 space-y-4">
          <select
            value={boardId}
            onChange={(e) => setBoardId(e.target.value)}
            className={`${inputClass} cursor-pointer`}
            style={selectStyle}
          >
            <option value="">{t("noBoardOption")}</option>
            {boards.map((b) => (
              <option key={b.id} value={b.id}>{b.name}</option>
            ))}
          </select>

          <div className="flex justify-end gap-2">
            <button onClick={onClose} className={btnCancelClass}>
              {t("cancel")}
            </button>
            <button
              onClick={handleAssign}
              className="px-5 py-2.5 text-sm rounded-sm font-semibold cursor-pointer transition-all hover:brightness-110"
              style={btnPrimaryStyle}
            >
              {t("assign")}
            </button>
          </div>
        </div>
      </motion.div>
    </div>
  );
}

// ── Templates Tab ───────────────────────────────────────────────────────────

function TemplatesTab({
  onUseTemplate,
}: {
  onUseTemplate: (templateId: string) => void;
}) {
  const t = useTranslations("agents");
  const { data: templates, isLoading } = useQuery({
    queryKey: ["agent-templates"],
    queryFn: api.agentTemplates.list,
    staleTime: 60_000,
  });

  if (isLoading) {
    return (
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        {[...Array(4)].map((_, i) => (
          <GlassCard key={i} className="p-4 animate-pulse">
            <div className="h-36 rounded-lg bg-[var(--color-bg-elevated)]" />
          </GlassCard>
        ))}
      </div>
    );
  }

  return (
    <>
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        {(templates ?? []).map((tmpl, i) => (
          <motion.div
            key={tmpl.id}
            initial={{ opacity: 0, y: 12 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.25, delay: i * 0.04, ease }}
          >
            <GlassCard className="p-4 flex flex-col gap-3 h-full">
              <div className="flex items-start justify-between">
                <div>
                  <div className="flex items-center gap-2">
                    <span className="text-xl">{tmpl.emoji}</span>
                    <span className="text-[15px] font-semibold text-[var(--color-text-primary)]">
                      {tmpl.name}
                    </span>
                    {tmpl.is_builtin && (
                      <Pill color={C.accent} size="sm">
                        builtin
                      </Pill>
                    )}
                  </div>
                  {tmpl.role && (
                    <p className="text-[11px] text-[var(--color-text-muted)] mt-1 ml-[calc(1.25rem+0.5rem)]">
                      {tmpl.role}
                    </p>
                  )}
                </div>
              </div>

              {tmpl.default_model && (
                <div className="text-[11px] font-mono text-[var(--color-text-muted)]">
                  {t("modelLabel")}{" "}
                  <span className="text-[var(--color-text-secondary)]">
                    {tmpl.default_model.split("/").pop()}
                  </span>
                </div>
              )}

              {(tmpl.skills?.length ?? 0) > 0 && (
                <div className="flex flex-wrap gap-1">
                  {tmpl.skills!.slice(0, 5).map((s) => (
                    <span
                      key={s}
                      className="text-[10px] px-1.5 py-0.5 rounded-sm font-mono"
                      style={{
                        backgroundColor: "var(--color-bg-elevated)",
                        color: "var(--color-text-muted)",
                        border: `1px solid ${CINEMA.borderSubtle}`,
                      }}
                    >
                      {s}
                    </span>
                  ))}
                  {tmpl.skills!.length > 5 && (
                    <span className="text-[10px] text-[var(--color-text-muted)]">
                      +{tmpl.skills!.length - 5}
                    </span>
                  )}
                </div>
              )}

              <div className="mt-auto pt-2">
                <button
                  onClick={() => onUseTemplate(tmpl.id)}
                  className="w-full text-xs px-3 py-2 rounded-xl font-medium cursor-pointer transition-all text-[var(--color-on-accent)]"
                  style={btnPrimaryStyle}
                >
                  {t("createAgent")}
                </button>
              </div>
            </GlassCard>
          </motion.div>
        ))}

        {!templates?.length && (
          <div className="col-span-3 py-12 text-center text-sm text-[var(--color-text-muted)]">
            {t("noTemplatesFound")}
          </div>
        )}
      </div>
    </>
  );
}

// ── Agent List Card (for Agents tab — richer than AgentGrid card) ───────────

// ── Roster (command-center list) ────────────────────────────────────────────
// Operator (11.06.2026): cards → dense roster list. One row per agent,
// actions in a ⋮ sheet, row tap opens the detail (stretched-link pattern —
// the name link covers the row via ::after, no button-in-button).

const DOT_STATUS = (status: string) => {
  switch (status) {
    case "online": return "online" as const;
    case "busy": return "busy" as const;
    case "error": return "error" as const;
    case "restarting": return "warning" as const;
    case "idle": return "idle" as const;
    default: return "offline" as const;
  }
};

// labelKey resolves via t() at the render site (agents.* namespace).
const PROVISION_MAP: Record<string, { labelKey: string; color: string }> = {
  local: { labelKey: "provLocal", color: C.textDim },
  provisioning: { labelKey: "provProvisioning", color: C.warning },
  provisioned: { labelKey: "provLive", color: C.online },
  error: { labelKey: "provError", color: C.error },
};

function ContextBar({ pct }: { pct: number }) {
  const t = useTranslations("agents");
  const color = pct >= 90 ? C.error : pct >= 70 ? C.warning : C.info;
  return (
    <span className="flex items-center gap-1.5 shrink-0" title={t("contextPct", { pct })}>
      <span
        className="h-1 w-10 sm:w-14 rounded-full overflow-hidden"
        style={{ backgroundColor: "var(--color-bg-elevated)" }}
      >
        <span
          className="block h-full rounded-full transition-[width] duration-500"
          style={{ width: `${Math.min(pct, 100)}%`, backgroundColor: color }}
        />
      </span>
      <span
        className="text-[10px] tabular-nums w-8 text-right"
        style={{ color: pct >= 70 ? color : C.textMuted }}
      >
        {pct}%
      </span>
    </span>
  );
}

function AgentRosterRow({
  agent,
  boardName,
  showAllAgents,
  onMenu,
}: {
  agent: Agent;
  boardName: string | null;
  showAllAgents: boolean;
  onMenu: (a: Agent) => void;
}) {
  const t = useTranslations("agents");
  const locale = useLocale();
  const pct = contextPercent(agent.context_tokens, agent.context_max);
  const prov = PROVISION_MAP[agent.provision_status] ?? PROVISION_MAP.local;
  const model = agent.model ? agent.model.split("/").pop() : null;
  const dot = DOT_STATUS(agent.status);

  return (
    <div className="relative flex items-center gap-2.5 sm:gap-3 px-3 sm:px-4 min-h-[56px] py-2 transition-colors hover:bg-[var(--color-bg-hover)]">
      <StatusDot status={dot} pulse={dot === "online" || dot === "busy"} />
      <span className="leading-none shrink-0 w-6 text-center" aria-hidden>
        <EntityIcon value={agent.emoji} size={18} className="inline-block" />
      </span>

      {/* Name + role = row link (covers the row via ::after) */}
      <Link
        href={`/agents/${agent.id}`}
        aria-label={t("openAgent", { name: agent.name })}
        className="min-w-0 flex-1 after:absolute after:inset-0 after:content-['']"
      >
        <span className="flex items-center gap-2 min-w-0">
          <span
            className="text-[13px] font-semibold truncate"
            style={{ color: "var(--color-text-primary)" }}
          >
            {agent.name}
          </span>
          {agent.provision_status !== "provisioned" && (
            <Pill color={prov.color} size="sm">{t(prov.labelKey)}</Pill>
          )}
          {agent.harness && (
            // "hermes" (ADR-060, host-only) has no HARNESS_LABELS entry — fall
            // back to the raw value instead of indexing out of bounds.
            // v3: SVG-Marke statt ausgeschriebenem CLI-Namen; Mobile ausgeblendet.
            <span
              className="max-sm:hidden inline-flex items-center justify-center w-6 h-6 rounded-sm shrink-0"
              style={{
                color: C.textMuted,
                backgroundColor: `${C.textMuted}14`,
                border: `1px solid ${C.textMuted}26`,
              }}
              title={harnessLabel(agent.harness)}
              aria-label={harnessLabel(agent.harness)}
            >
              <HarnessIcon harness={agent.harness} size={12} />
            </span>
          )}
          {showAllAgents && (
            <span
              className="text-[9px] px-1.5 py-0.5 rounded-sm font-mono shrink-0 max-sm:hidden"
              style={{
                color: boardName ? C.textMuted : C.warning,
                border: `1px solid ${boardName ? CINEMA.borderSubtle : `${C.warning}4D`}`,
              }}
            >
              {boardName ?? t("noBoard")}
            </span>
          )}
        </span>
        {agent.role && (
          <span className="block text-[10px] truncate mt-0.5" style={{ color: C.textMuted }}>
            {agent.role}
          </span>
        )}
      </Link>

      {/* Metric columns */}
      {model && (
        <span
          className="font-mono text-[10px] shrink-0 max-md:hidden"
          style={{ color: C.textMuted }}
          title={agent.model ?? undefined}
        >
          {/* Middle-ellipsis: the tail (quant/runtime suffix) is what tells
              models apart — end-truncation hid exactly that. */}
          {model.length > 26 ? `${model.slice(0, 12)}…${model.slice(-12)}` : model}
        </span>
      )}
      <span className="text-[10px] shrink-0 max-lg:hidden" style={{ color: C.textDim }}>
        HB {agent.heartbeat_config?.interval ?? "5m"}
      </span>
      <ContextBar pct={pct} />
      <span
        className="text-[11px] tabular-nums w-9 text-right shrink-0 max-sm:hidden"
        style={{ color: "var(--color-text-secondary)" }}
        title={t("tasksCompleted", { count: agent.total_tasks_completed })}
      >
        {agent.total_tasks_completed}
      </span>

      {/* Actions — above the row overlay */}
      <button
        onClick={() => onMenu(agent)}
        aria-label={t("actionsFor", { name: agent.name })}
        className="relative z-[1] flex items-center justify-center w-9 h-9 min-h-touch rounded-lg shrink-0 cursor-pointer transition-colors hover:bg-[var(--color-bg-hover)]"
        style={{ color: C.textMuted }}
      >
        <MoreVertical size={15} />
      </button>
    </div>
  );
}

function AgentActionsSheet({
  agent,
  boardName,
  showAllAgents,
  resettingId,
  onReset,
  onArchive,
  onAssignBoard,
  onClose,
}: {
  agent: Agent;
  boardName: string | null;
  showAllAgents: boolean;
  resettingId: string | null;
  onReset: (a: Agent) => void;
  onArchive: (a: Agent) => void;
  onAssignBoard: (a: Agent) => void;
  onClose: () => void;
}) {
  const t = useTranslations("agents");
  const locale = useLocale();
  useBodyScrollLock(true);
  // Esc closes (panel register rule 4) — backdrop click is below.
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [onClose]);
  const pct = contextPercent(agent.context_tokens, agent.context_max);
  const displaySkills = agent.skill_filter ?? agent.skills ?? [];
  const dot = DOT_STATUS(agent.status);

  const itemCls =
    "flex items-center gap-3 w-full px-4 py-3 min-h-touch text-[13px] text-left rounded-lg transition-colors cursor-pointer hover:bg-[var(--color-bg-hover)]";

  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      transition={{ duration: 0.15 }}
      className="fixed inset-0 z-50 flex items-end sm:items-center justify-center sm:px-4"
      style={{ background: "rgba(0,0,0,0.6)" }}
      onClick={onClose}
    >
      <motion.div
        role="dialog"
        aria-modal="true"
        aria-label={t("actionsFor", { name: agent.name })}
        initial={{ opacity: 0, y: 32 }}
        animate={{ opacity: 1, y: 0 }}
        exit={{ opacity: 0, y: 32 }}
        transition={{ duration: 0.22, ease }}
        className="w-full sm:max-w-sm rounded-t-2xl sm:rounded-2xl overflow-hidden max-h-[92dvh] flex flex-col"
        style={{
          backgroundColor: C.bgBase,
          border: `1px solid ${C.border}`,
          boxShadow: "0 4px 24px rgba(0,0,0,0.5), 0 1px 2px rgba(0,0,0,0.3)",
        }}
        onClick={(e) => e.stopPropagation()}
      >
        {/* Drag indicator (mobile) */}
        <div className="sm:hidden flex justify-center pt-2.5 shrink-0">
          <div className="w-8 h-1 rounded-full" style={{ backgroundColor: "var(--color-bg-hover)" }} />
        </div>

        {/* Header */}
        <div className="px-4 pt-3 pb-3" style={{ borderBottom: `1px solid ${C.border}` }}>
          <div className="flex items-center gap-2.5">
            <EntityIcon value={agent.emoji} size={20} />
            <div className="min-w-0 flex-1">
              <div className="text-sm font-semibold truncate" style={{ color: "var(--color-text-primary)" }}>
                {agent.name}
              </div>
              <div className="flex items-center gap-2 text-[10px]" style={{ color: C.textMuted }}>
                <StatusDot status={dot} />
                <span className="capitalize">{agent.status}</span>
                <span>· {t("contextPct", { pct })}</span>
                {agent.last_seen_at && <span>· {timeAgo(agent.last_seen_at, locale)}</span>}
              </div>
            </div>
          </div>
          {displaySkills.length > 0 && (
            <div className="mt-2">
              <SkillBadges skills={displaySkills} />
            </div>
          )}
        </div>

        {/* Actions */}
        <div
          className="flex flex-col p-2 overflow-y-auto"
          style={{ paddingBottom: "calc(env(safe-area-inset-bottom) + 0.5rem)" }}
        >
          <Link href={`/agents/${agent.id}`} className={itemCls} style={{ color: "var(--color-text-primary)" }}>
            <Bot size={15} style={{ color: C.accent }} /> {t("openDetails")}
          </Link>
          <Link href={`/agents/${agent.id}?tab=config`} className={itemCls} style={{ color: "var(--color-text-secondary)" }}>
            <Settings size={15} /> {t("config")}
          </Link>
          <Link href={`/agents/${agent.id}?tab=analytics`} className={itemCls} style={{ color: "var(--color-text-secondary)" }}>
            <BarChart3 size={15} /> {t("analytics")}
          </Link>
          <button
            onClick={() => { onClose(); onReset(agent); }}
            disabled={resettingId === agent.id}
            className={itemCls + " disabled:opacity-50"}
            style={{ color: "var(--color-text-secondary)" }}
          >
            {resettingId === agent.id ? <Loader2 size={15} className="animate-spin" /> : <RotateCcw size={15} />}
            {t("sessionReset")}
          </button>
          {showAllAgents && (
            <button
              onClick={() => { onClose(); onAssignBoard(agent); }}
              className={itemCls}
              style={{ color: "var(--color-text-secondary)" }}
            >
              <Layout size={15} /> {t("assignBoard")}{boardName ? ` (${boardName})` : ""}
            </button>
          )}
          <button
            onClick={() => { onClose(); onArchive(agent); }}
            className={itemCls}
            style={{ color: C.warning }}
          >
            <Archive size={15} /> {t("archive")}
          </button>
        </div>
      </motion.div>
    </motion.div>
  );
}

// ── Agents Page ─────────────────────────────────────────────────────────────

export default function AgentsPage() {
  const t = useTranslations("agents");
  const locale = useLocale();
  const qc = useQueryClient();
  const { activeBoardId } = useAppStore();

  const [activeTab, setActiveTab] = useState<"agents" | "templates">("agents");
  const [wizardOpen, setWizardOpen] = useState(false);
  const [wizardInitial, setWizardInitial] = useState<Partial<WizardState> | undefined>(undefined);
  const [showAllAgents, setShowAllAgents] = useState(false);
  const [assignBoardAgent, setAssignBoardAgent] = useState<Agent | null>(null);
  const [menuAgent, setMenuAgent] = useState<Agent | null>(null);
  const [resettingId, setResettingId] = useState<string | null>(null);

  // SSE: refresh agents on events
  useAgentStream((eventType) => {
    if (
      eventType?.startsWith("agent.") ||
      eventType === "task.status_changed" ||
      eventType === "task.assigned"
    ) {
      qc.invalidateQueries({ queryKey: ["agents"] });
    }
  });

  const { data: agents, isLoading } = useQuery({
    queryKey: ["agents", activeBoardId, showAllAgents],
    queryFn: () =>
      showAllAgents
        ? api.agents.list(undefined, false)
        : api.agents.list(activeBoardId ?? undefined),
    refetchInterval: 30_000,
  });

  const { data: boards } = useQuery({
    queryKey: ["boards"],
    queryFn: () => api.boards.list(),
  });

  // Archived agents (registry-wide, incl. board-less). include_archived returns
  // active + archived, so filter down to the tombstoned ones for the section.
  const { data: archivedRaw } = useQuery({
    queryKey: ["agents", "archived"],
    queryFn: () => api.agents.list(undefined, true, true),
    refetchInterval: 60_000,
  });
  const archivedAgents = useMemo(
    () => (archivedRaw ?? []).filter((a) => a.archived_at != null),
    [archivedRaw]
  );

  const boardsMap = useMemo(
    () => Object.fromEntries((boards ?? []).map((b) => [b.id, b.name])),
    [boards]
  );

  // ── Actions ─────────────────────────────────────────────────────────────
  const handleReset = async (agent: Agent) => {
    setResettingId(agent.id);
    try {
      await api.agents.reset(agent.id);
      notify.success(t("sessionResetDone", { name: agent.name }));
      qc.invalidateQueries({ queryKey: ["agents"] });
    } catch {
      notify.error(t("sessionResetFailed", { name: agent.name }));
    } finally {
      setResettingId(null);
    }
  };

  // Active agents are archived (not hard-deleted) from the roster — hard delete
  // is gated on archived state (backend 409). Surface 409 (busy) / 422
  // (singleton bridge) detail verbatim instead of a generic toast.
  const archiveMutation = useMutation({
    mutationFn: (agent: Agent) => api.agents.archive(agent.id),
    onSuccess: (_res, agent) => {
      notify.success(t("archivedNotify", { name: agent.name }));
      qc.invalidateQueries({ queryKey: ["agents"] });
    },
    onError: (e) => notify.error(extractDetail(e)),
  });

  // "online" = alive (heartbeating): idle/working count too — previously the
  // list showed 0/14 even though the whole fleet was running (idle was ignored).
  const ALIVE = new Set(["online", "busy", "idle", "working"]);
  const onlineCount = agents?.filter((a) => ALIVE.has(a.status)).length ?? 0;
  const totalCount = agents?.length ?? 0;

  return (
    <AppShell>
      <div className="space-y-6">
        {/* Header */}
        <div className="flex items-center justify-between gap-4">
          <div>
            <div className="label-sys mb-2">{t("fleetAgents")}</div>
            <h1 className="display text-2xl font-semibold text-[var(--color-text-primary)]">
              {t("title")}
            </h1>
            <p className="text-[13px] text-[var(--color-text-secondary)] mt-1">
              {t("onlineCount", { online: onlineCount, total: totalCount })}
            </p>
          </div>

          <div className="flex items-center gap-2">
            <button
              onClick={() => { setWizardInitial(undefined); setWizardOpen(true); }}
              className="flex items-center gap-2 px-3.5 py-2 text-sm rounded-sm font-semibold cursor-pointer transition-all hover:brightness-110"
              style={btnPrimaryStyle}
            >
              <Plus size={14} />
              {t("newAgent")}
            </button>
          </div>
        </div>

        {/* Tab header — .tab-strip: mobile horizontal scroll + edge-fade (MOBILE-SPEC M17) */}
        <div
          className="flex items-center gap-1 border-b tab-strip"
          style={{ borderColor: CINEMA.borderSubtle }}
        >
          <button
            onClick={() => setActiveTab("agents")}
            className="px-4 py-2.5 text-sm font-medium transition-colors cursor-pointer min-h-touch"
            style={{
              color: activeTab === "agents"
                ? "var(--color-text-primary)"
                : "var(--color-text-muted)",
              borderBottom: activeTab === "agents"
                ? `2px solid ${C.accent}`
                : "2px solid transparent",
              marginBottom: "-1px",
            }}
          >
            <span className="flex items-center gap-2">
              <Bot size={14} />
              {t("title")} ({totalCount})
            </span>
          </button>
          <button
            onClick={() => setActiveTab("templates")}
            className="px-4 py-2.5 text-sm font-medium transition-colors cursor-pointer min-h-touch"
            style={{
              color: activeTab === "templates"
                ? "var(--color-text-primary)"
                : "var(--color-text-muted)",
              borderBottom: activeTab === "templates"
                ? `2px solid ${C.accent}`
                : "2px solid transparent",
              marginBottom: "-1px",
            }}
          >
            <span className="flex items-center gap-2">
              <Users size={14} />
              {t("templates")}
            </span>
          </button>
        </div>

        {/* Tab: Agents */}
        {activeTab === "agents" && (
          <div className="space-y-4">
            {/* Board filter toggle */}
            <div className="flex items-center gap-2">
              <button
                onClick={() => setShowAllAgents(!showAllAgents)}
                className="flex items-center gap-1.5 text-[11px] px-3 py-1.5 max-sm:min-h-touch rounded-xl transition-colors cursor-pointer"
                style={{
                  backgroundColor: showAllAgents ? C.accentSubtle : "var(--color-bg-elevated)",
                  color: showAllAgents ? C.accent : "var(--color-text-muted)",
                  border: `1px solid ${showAllAgents ? C.borderAccent : CINEMA.borderSubtle}`,
                }}
              >
                <Layout size={12} />
                {showAllAgents ? t("allAgents") : t("thisBoardOnly")}
                <ChevronDown
                  size={12}
                  className="transition-transform"
                  style={{ transform: showAllAgents ? "rotate(180deg)" : "rotate(0deg)" }}
                />
              </button>
              {showAllAgents && (
                <span className="text-[11px] text-[var(--color-text-muted)]">
                  {t("registryViewHint")}
                </span>
              )}
            </div>

            {/* Roster — a flat list instead of cards (command center) */}
            {isLoading ? (
              <div
                className="rounded-xl overflow-hidden animate-pulse"
                style={{ backgroundColor: C.bgSurface, border: `1px solid ${C.border}` }}
              >
                {[...Array(5)].map((_, i) => (
                  <div
                    key={i}
                    className="h-[56px]"
                    style={{ borderTop: i > 0 ? `1px solid ${C.borderSubtle}` : undefined }}
                  />
                ))}
              </div>
            ) : agents?.length ? (
              <div
                className="rounded-xl overflow-hidden"
                style={{ backgroundColor: C.bgSurface, border: `1px solid ${C.border}` }}
              >
                {(agents ?? []).map((agent, i) => (
                  <div key={agent.id} style={{ borderTop: i > 0 ? `1px solid ${C.borderSubtle}` : undefined }}>
                    <AgentRosterRow
                      agent={agent}
                      boardName={agent.board_id ? boardsMap[agent.board_id] ?? null : null}
                      showAllAgents={showAllAgents}
                      onMenu={setMenuAgent}
                    />
                  </div>
                ))}
              </div>
            ) : (
              <GlassCard className="py-16 text-center">
                <p className="text-sm text-[var(--color-text-muted)]">
                  {showAllAgents
                    ? t("noAgentsFound")
                    : t("noAgentsForBoard")}
                </p>
              </GlassCard>
            )}

            {/* Archiviert — tombstoned agents (runtime stopped, DB+files kept).
                Muted, below the active fleet. Restore brings them back; Delete
                is the one irreversible path (enabled only here). */}
            {archivedAgents.length > 0 && (
              <div className="pt-2">
                <div className="flex items-center gap-2 mb-2 px-1">
                  <Archive size={13} style={{ color: C.textMuted }} />
                  <h2 className="text-[11px] font-medium uppercase tracking-[0.05em]" style={{ color: C.textMuted }}>
                    {t("archivedCount", { count: archivedAgents.length })}
                  </h2>
                </div>
                <div
                  className="rounded-xl overflow-hidden"
                  style={{ backgroundColor: C.bgBase, border: `1px solid ${C.borderSubtle}`, opacity: 0.85 }}
                >
                  {archivedAgents.map((agent, i) => (
                    <div
                      key={agent.id}
                      className="flex items-center gap-3 px-3 sm:px-4 min-h-[52px] py-2 flex-wrap"
                      style={{ borderTop: i > 0 ? `1px solid ${C.borderSubtle}` : undefined }}
                    >
                      <span className="leading-none shrink-0 w-6 text-center opacity-70" aria-hidden>
                        <EntityIcon value={agent.emoji} size={16} className="inline-block" />
                      </span>
                      <Link href={`/agents/${agent.id}`} className="min-w-0 flex-1">
                        <span className="text-[13px] font-medium truncate block" style={{ color: C.textSecondary }}>
                          {agent.name}
                        </span>
                        {agent.archived_at && (
                          <span className="block text-[10px] truncate mt-0.5" style={{ color: C.textMuted }}>
                            {t("archivedAgo", { ago: timeAgo(agent.archived_at, locale) })}
                          </span>
                        )}
                      </Link>
                      <AgentActions agent={agent} />
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}

        {/* Tab: Templates */}
        {activeTab === "templates" && (
          <TemplatesTab
            onUseTemplate={(id) => {
              setWizardInitial({ startMode: "template", templateId: id, step: 0 });
              setWizardOpen(true);
            }}
          />
        )}

        {/* ── Modals ──────────────────────────────────────────────────────────── */}
        <AnimatePresence>
          {menuAgent && (
            <AgentActionsSheet
              agent={menuAgent}
              boardName={menuAgent.board_id ? boardsMap[menuAgent.board_id] ?? null : null}
              showAllAgents={showAllAgents}
              resettingId={resettingId}
              onReset={handleReset}
              onArchive={(a) => archiveMutation.mutate(a)}
              onAssignBoard={setAssignBoardAgent}
              onClose={() => setMenuAgent(null)}
            />
          )}
        </AnimatePresence>

        <AnimatePresence>
          {wizardOpen && (
            <AgentWizard
              boards={boards ?? []}
              defaultBoardId={activeBoardId}
              initialState={wizardInitial}
              onClose={() => setWizardOpen(false)}
              onCreated={() => setWizardOpen(false)}
            />
          )}
        </AnimatePresence>

        <AnimatePresence>
          {assignBoardAgent && (
            <AssignBoardModal
              agent={assignBoardAgent}
              boards={boards ?? []}
              onClose={() => setAssignBoardAgent(null)}
            />
          )}
        </AnimatePresence>
      </div>
    </AppShell>
  );
}
