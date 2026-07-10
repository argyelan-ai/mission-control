"use client";

import { useState, useMemo } from "react";
import Link from "next/link";
import AppShell from "@/components/layout/AppShell";
import { useQuery, useQueryClient, useMutation } from "@tanstack/react-query";
import { motion, AnimatePresence } from "framer-motion";
import {
  Plus, X, Loader2, Bot, Users, Zap, RotateCcw, Settings, BarChart3,
  Layout, ChevronDown, Trash2, MoreVertical,
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
import { HARNESS_LABELS } from "@/lib/types";
import { AgentWizard } from "./wizard/AgentWizard";
import type { WizardState } from "./wizard/types";

// ── Design Tokens (migrated from CINEMA inline map → lib/colors.ts) ────────
const CINEMA = {
  modalBg: C.bgBase,
  border: C.border,
  borderSubtle: C.borderSubtle,
  surfaceBg: "rgba(255,255,255,0.03)",
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
  background: `linear-gradient(135deg, ${C.accentHover}, ${C.accent})`,
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
  const [boardId, setBoardId] = useState(agent.board_id ?? "");
  const qc = useQueryClient();

  async function handleAssign() {
    try {
      await api.agents.assignBoard(agent.id, boardId || null);
      notify.success(`${agent.name} assigned to board`);
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
            {agent.emoji ?? "🤖"} {agent.name} — Assign board
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
            <option value="">-- No board --</option>
            {boards.map((b) => (
              <option key={b.id} value={b.id}>{b.name}</option>
            ))}
          </select>

          <div className="flex justify-end gap-2">
            <button onClick={onClose} className={btnCancelClass}>
              Cancel
            </button>
            <button
              onClick={handleAssign}
              className="px-5 py-2.5 text-sm rounded-xl font-medium text-white cursor-pointer transition-all"
              style={btnPrimaryStyle}
            >
              Assign
            </button>
          </div>
        </div>
      </motion.div>
    </div>
  );
}

// ── Delete Confirmation Modal ───────────────────────────────────────────────

function DeleteAgentModal({
  agent,
  onClose,
  onConfirm,
  isPending,
}: {
  agent: Agent;
  onClose: () => void;
  onConfirm: () => void;
  isPending: boolean;
}) {
  return (
    <div className={modalOverlayClass} onClick={onClose}>
      <div className={modalBackdropClass} />
      <motion.div
        initial={{ opacity: 0, scale: 0.95, y: 8 }}
        animate={{ opacity: 1, scale: 1, y: 0 }}
        exit={{ opacity: 0, scale: 0.95, y: 8 }}
        transition={{ duration: 0.2, ease }}
        className="relative w-full max-w-sm rounded-t-2xl sm:rounded-2xl overflow-hidden p-6 max-h-[90dvh] overflow-y-auto"
        style={modalCardStyle}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-3 mb-4">
          <div
            className="w-10 h-10 rounded-full flex items-center justify-center text-lg"
            style={{ backgroundColor: CINEMA.errorBg }}
          >
            <Trash2 size={18} className="text-[var(--color-status-error)]" />
          </div>
          <div>
            <div className="font-medium text-[var(--color-text-primary)]">Delete agent?</div>
            <div className="text-sm text-[var(--color-text-muted)]">
              {agent.emoji} {agent.name}
            </div>
          </div>
        </div>

        <p className="text-sm mb-2 text-[var(--color-text-secondary)]">
          This action cannot be undone. Chat history and metrics will
          also be lost.
        </p>

        {agent.provision_status === "provisioned" && (
          <p
            className="text-xs mb-4 px-3 py-2 rounded-xl"
            style={{
              backgroundColor: CINEMA.warningBg,
              color: "var(--color-status-warning)",
              border: `1px solid ${CINEMA.warningBorder}`,
            }}
          >
            Gateway session will be reset.
          </p>
        )}

        <div className="flex gap-3 mt-4">
          <button
            onClick={onClose}
            className="flex-1 px-4 py-2.5 rounded-xl text-sm cursor-pointer transition-colors text-[var(--color-text-secondary)]"
            style={{
              backgroundColor: CINEMA.surfaceBg,
              border: `1px solid ${CINEMA.border}`,
            }}
          >
            Cancel
          </button>
          <button
            onClick={onConfirm}
            disabled={isPending}
            className="flex-1 flex items-center justify-center gap-2 px-4 py-2.5 rounded-xl text-sm font-medium cursor-pointer transition-colors disabled:opacity-50 text-white"
            style={{ backgroundColor: C.error }}
          >
            {isPending ? <Loader2 size={14} className="animate-spin" /> : <Trash2 size={14} />}
            Delete
          </button>
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
            <div className="h-36 rounded-lg bg-[rgba(255,255,255,0.03)]" />
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
                  Model:{" "}
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
                      className="text-[10px] px-1.5 py-0.5 rounded-full"
                      style={{
                        backgroundColor: "rgba(255,255,255,0.05)",
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
                  className="w-full text-xs px-3 py-2 rounded-xl font-medium cursor-pointer transition-all text-white"
                  style={btnPrimaryStyle}
                >
                  Create agent
                </button>
              </div>
            </GlassCard>
          </motion.div>
        ))}

        {!templates?.length && (
          <div className="col-span-3 py-12 text-center text-sm text-[var(--color-text-muted)]">
            No templates found.
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

const PROVISION_MAP: Record<string, { label: string; color: string }> = {
  local: { label: "Local", color: C.textDim },
  provisioning: { label: "Provisioning", color: C.warning },
  provisioned: { label: "Live", color: C.online },
  error: { label: "Error", color: C.error },
};

function ContextBar({ pct }: { pct: number }) {
  const color = pct >= 90 ? C.error : pct >= 70 ? C.warning : C.info;
  return (
    <span className="flex items-center gap-1.5 shrink-0" title={`Context: ${pct}%`}>
      <span
        className="h-1 w-10 sm:w-14 rounded-full overflow-hidden"
        style={{ backgroundColor: "rgba(255,255,255,0.06)" }}
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
  const pct = contextPercent(agent.context_tokens, agent.context_max);
  const prov = PROVISION_MAP[agent.provision_status] ?? PROVISION_MAP.local;
  const model = agent.model ? agent.model.split("/").pop() : null;
  const dot = DOT_STATUS(agent.status);

  return (
    <div className="relative flex items-center gap-2.5 sm:gap-3 px-3 sm:px-4 min-h-[56px] py-2 transition-colors hover:bg-[rgba(255,255,255,0.03)]">
      <StatusDot status={dot} pulse={dot === "online" || dot === "busy"} />
      <span className="text-lg leading-none shrink-0 w-6 text-center" aria-hidden>
        {agent.emoji ?? "🤖"}
      </span>

      {/* Name + role = row link (covers the row via ::after) */}
      <Link
        href={`/agents/${agent.id}`}
        aria-label={`Open agent: ${agent.name}`}
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
            <Pill color={prov.color} size="sm">{prov.label}</Pill>
          )}
          {agent.harness && (
            <Pill color={C.textMuted} size="sm">{HARNESS_LABELS[agent.harness]}</Pill>
          )}
          {showAllAgents && (
            <span
              className="text-[9px] px-1.5 py-0.5 rounded-full shrink-0 max-sm:hidden"
              style={{
                color: boardName ? C.textMuted : C.warning,
                border: `1px solid ${boardName ? CINEMA.borderSubtle : `${C.warning}4D`}`,
              }}
            >
              {boardName ?? "No board"}
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
        title={`${agent.total_tasks_completed} tasks completed`}
      >
        {agent.total_tasks_completed}
      </span>

      {/* Actions — above the row overlay */}
      <button
        onClick={() => onMenu(agent)}
        aria-label={`Actions for ${agent.name}`}
        className="relative z-[1] flex items-center justify-center w-9 h-9 min-h-touch rounded-lg shrink-0 cursor-pointer transition-colors hover:bg-[rgba(255,255,255,0.06)]"
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
  triggeringId,
  resettingId,
  onTrigger,
  onReset,
  onDelete,
  onAssignBoard,
  onClose,
}: {
  agent: Agent;
  boardName: string | null;
  showAllAgents: boolean;
  triggeringId: string | null;
  resettingId: string | null;
  onTrigger: (a: Agent) => void;
  onReset: (a: Agent) => void;
  onDelete: (a: Agent) => void;
  onAssignBoard: (a: Agent) => void;
  onClose: () => void;
}) {
  useBodyScrollLock(true);
  const pct = contextPercent(agent.context_tokens, agent.context_max);
  const displaySkills = agent.skill_filter ?? agent.skills ?? [];
  const dot = DOT_STATUS(agent.status);

  const itemCls =
    "flex items-center gap-3 w-full px-4 py-3 min-h-touch text-[13px] text-left rounded-lg transition-colors cursor-pointer hover:bg-[rgba(255,255,255,0.05)]";

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
        aria-label={`Actions: ${agent.name}`}
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
          <div className="w-8 h-1 rounded-full" style={{ backgroundColor: "rgba(255,255,255,0.18)" }} />
        </div>

        {/* Header */}
        <div className="px-4 pt-3 pb-3" style={{ borderBottom: `1px solid ${C.border}` }}>
          <div className="flex items-center gap-2.5">
            <span className="text-xl leading-none" aria-hidden>{agent.emoji ?? "🤖"}</span>
            <div className="min-w-0 flex-1">
              <div className="text-sm font-semibold truncate" style={{ color: "var(--color-text-primary)" }}>
                {agent.name}
              </div>
              <div className="flex items-center gap-2 text-[10px]" style={{ color: C.textMuted }}>
                <StatusDot status={dot} />
                <span className="capitalize">{agent.status}</span>
                <span>· Context {pct}%</span>
                {agent.last_seen_at && <span>· {timeAgo(agent.last_seen_at)}</span>}
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
            <Bot size={15} style={{ color: C.accent }} /> Open details
          </Link>
          <Link href={`/agents/${agent.id}?tab=config`} className={itemCls} style={{ color: "var(--color-text-secondary)" }}>
            <Settings size={15} /> Config
          </Link>
          <Link href={`/agents/${agent.id}?tab=analytics`} className={itemCls} style={{ color: "var(--color-text-secondary)" }}>
            <BarChart3 size={15} /> Analytics
          </Link>
          <button
            onClick={() => { onClose(); onTrigger(agent); }}
            disabled={triggeringId === agent.id}
            className={itemCls + " disabled:opacity-50"}
            style={{ color: C.info }}
          >
            {triggeringId === agent.id ? <Loader2 size={15} className="animate-spin" /> : <Zap size={15} />}
            Trigger
          </button>
          <button
            onClick={() => { onClose(); onReset(agent); }}
            disabled={resettingId === agent.id}
            className={itemCls + " disabled:opacity-50"}
            style={{ color: "var(--color-text-secondary)" }}
          >
            {resettingId === agent.id ? <Loader2 size={15} className="animate-spin" /> : <RotateCcw size={15} />}
            Session Reset
          </button>
          {showAllAgents && (
            <button
              onClick={() => { onClose(); onAssignBoard(agent); }}
              className={itemCls}
              style={{ color: "var(--color-text-secondary)" }}
            >
              <Layout size={15} /> Assign board{boardName ? ` (${boardName})` : ""}
            </button>
          )}
          <button
            onClick={() => { onClose(); onDelete(agent); }}
            className={itemCls}
            style={{ color: C.error }}
          >
            <Trash2 size={15} /> Delete agent
          </button>
        </div>
      </motion.div>
    </motion.div>
  );
}

// ── Agents Page ─────────────────────────────────────────────────────────────

export default function AgentsPage() {
  const qc = useQueryClient();
  const { activeBoardId } = useAppStore();

  const [activeTab, setActiveTab] = useState<"agents" | "templates">("agents");
  const [wizardOpen, setWizardOpen] = useState(false);
  const [wizardInitial, setWizardInitial] = useState<Partial<WizardState> | undefined>(undefined);
  const [showAllAgents, setShowAllAgents] = useState(false);
  const [assignBoardAgent, setAssignBoardAgent] = useState<Agent | null>(null);
  const [deletingAgent, setDeletingAgent] = useState<Agent | null>(null);
  const [menuAgent, setMenuAgent] = useState<Agent | null>(null);
  const [triggeringId, setTriggeringId] = useState<string | null>(null);
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

  const boardsMap = useMemo(
    () => Object.fromEntries((boards ?? []).map((b) => [b.id, b.name])),
    [boards]
  );

  // ── Actions ─────────────────────────────────────────────────────────────
  const handleTrigger = async (agent: Agent) => {
    setTriggeringId(agent.id);
    try {
      const result = await api.agents.trigger(agent.id, "Please continue with your current task.");
      if (result.reply) {
        notify.success(`${agent.emoji ?? "🤖"} ${agent.name}: ${result.reply}`);
      } else {
        notify.info(`${agent.name}: Trigger sent, no reply received.`);
      }
      qc.invalidateQueries({ queryKey: ["agents"] });
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : "Unknown error";
      notify.error(`Trigger failed: ${msg}`);
    } finally {
      setTriggeringId(null);
    }
  };

  const handleReset = async (agent: Agent) => {
    setResettingId(agent.id);
    try {
      await api.agents.reset(agent.id);
      notify.success(`${agent.name} session reset`);
      qc.invalidateQueries({ queryKey: ["agents"] });
    } catch {
      notify.error(`Failed to reset ${agent.name}`);
    } finally {
      setResettingId(null);
    }
  };

  const deleteMutation = useMutation({
    mutationFn: (id: string) => api.agents.delete(id),
    onSuccess: () => {
      notify.success("Agent deleted");
      setDeletingAgent(null);
      qc.invalidateQueries({ queryKey: ["agents"] });
    },
    onError: () => {
      notify.error("Delete failed");
      setDeletingAgent(null);
    },
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
            <h1 className="text-2xl font-bold tracking-tight text-[var(--color-text-primary)]">
              Agents
            </h1>
            <p className="text-sm text-[var(--color-text-muted)] mt-1">
              {onlineCount}/{totalCount} online
            </p>
          </div>

          <div className="flex items-center gap-2">
            <button
              onClick={() => { setWizardInitial(undefined); setWizardOpen(true); }}
              className="flex items-center gap-2 px-3.5 py-2 text-sm rounded-xl font-medium text-white cursor-pointer transition-all"
              style={btnPrimaryStyle}
            >
              <Plus size={14} />
              Neuer Agent
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
              Agents ({totalCount})
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
              Templates
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
                className="flex items-center gap-1.5 text-[11px] px-3 py-1.5 rounded-xl transition-colors cursor-pointer"
                style={{
                  backgroundColor: showAllAgents ? C.accentSubtle : "rgba(255,255,255,0.04)",
                  color: showAllAgents ? C.accent : "var(--color-text-muted)",
                  border: `1px solid ${showAllAgents ? C.borderAccent : CINEMA.borderSubtle}`,
                }}
              >
                <Layout size={12} />
                {showAllAgents ? "All Agents" : "This board only"}
                <ChevronDown
                  size={12}
                  className="transition-transform"
                  style={{ transform: showAllAgents ? "rotate(180deg)" : "rotate(0deg)" }}
                />
              </button>
              {showAllAgents && (
                <span className="text-[11px] text-[var(--color-text-muted)]">
                  Registry view — shows all agents across all boards
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
                    ? "No agents found."
                    : "No agents configured for this board."}
                </p>
              </GlassCard>
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
              triggeringId={triggeringId}
              resettingId={resettingId}
              onTrigger={handleTrigger}
              onReset={handleReset}
              onDelete={setDeletingAgent}
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

        <AnimatePresence>
          {deletingAgent && (
            <DeleteAgentModal
              agent={deletingAgent}
              onClose={() => setDeletingAgent(null)}
              onConfirm={() => deleteMutation.mutate(deletingAgent.id)}
              isPending={deleteMutation.isPending}
            />
          )}
        </AnimatePresence>
      </div>
    </AppShell>
  );
}
