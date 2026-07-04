"use client";

import { useState } from "react";
import ReactMarkdown from "react-markdown";
import { motion } from "framer-motion";
import {
  CheckCircle, XCircle, AlertTriangle, ShieldAlert,
  Image as ImageIcon, ExternalLink, HelpCircle,
} from "lucide-react";
import { cn, timeAgo } from "@/lib/utils";
import { GlassCard } from "@/components/shared/GlassCard";
import { Pill } from "@/components/shared/Pill";
import { InstallRequestCard } from "./InstallRequestCard";
import { C } from "@/lib/colors";
import type { Approval, AutonomyLevel } from "@/lib/types";

// ── Install Action Types ──────────────────────────────────────────────────────

const INSTALL_ACTION_TYPES = new Set([
  "install_skill", "uninstall_skill",
  "install_plugin", "uninstall_plugin",
  "install_mcp", "uninstall_mcp",
]);

// ── Approval Type Config ─────────────────────────────────────────────────────

const APPROVAL_TYPE_CONFIG: Record<string, { label: string; color: string; icon: typeof AlertTriangle }> = {
  blocker_decision: {
    label: "Blocker",
    color: C.error,
    icon: AlertTriangle,
  },
  visual_review: {
    label: "Visual Review",
    color: C.accent,
    icon: ImageIcon,
  },
  dispatch_escalation: {
    label: "Agent not responding",
    color: C.warning,
    icon: AlertTriangle,
  },
  recovery_failed: {
    label: "Recovery failed",
    color: C.error,
    icon: ShieldAlert,
  },
  clarification_question: {
    label: "Clarification",
    color: C.accent,
    icon: HelpCircle,
  },
};

const AUTONOMY_COLORS: Record<string, string> = {
  L1: C.online,
  L2: C.warning,
  L3: C.error,
};

const BLOCKER_TYPE_LABELS: Record<string, { label: string; color: string }> = {
  missing_info: { label: "Fehlende Info", color: C.warning },
  technical_problem: { label: "Technisches Problem", color: C.error },
  decision_needed: { label: "Entscheidung nötig", color: C.accent },
  permission_needed: { label: "Berechtigung fehlt", color: C.warning },
  dependency_blocked: { label: "Abhängigkeit", color: C.textMuted },
  other: { label: "Sonstiges", color: C.textMuted },
};

// ── Approval Card ────────────────────────────────────────────────────────────

interface ApprovalCardProps {
  approval: Approval;
  onResolve: (status: "approved" | "rejected", note?: string) => void;
  loading?: boolean;
}

export function ApprovalCard({ approval, onResolve, loading }: ApprovalCardProps) {
  const [note, setNote] = useState("");

  // Dispatch install/uninstall variants to dedicated card
  if (INSTALL_ACTION_TYPES.has(approval.action_type)) {
    return (
      <InstallRequestCard
        approval={approval}
        onResolve={() => onResolve("approved")}
      />
    );
  }

  const isBlocker = approval.action_type === "blocker_decision";
  const typeConfig = APPROVAL_TYPE_CONFIG[approval.action_type];
  const label = typeConfig?.label ?? approval.action_type;
  const badgeColor = typeConfig?.color ?? C.textDim;
  const BadgeIcon = typeConfig?.icon ?? AlertTriangle;

  return (
    <motion.div
      layout
      initial={{ opacity: 0, x: -8 }}
      animate={{ opacity: 1, x: 0 }}
      exit={{ opacity: 0, x: 8, height: 0 }}
    >
      <GlassCard
        className="p-4"
        glow={`${badgeColor}12`}
      >
        {/* Header */}
        <div className="flex items-start justify-between gap-4">
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2 flex-wrap">
              <span
                className="text-[11px] px-2 py-0.5 rounded-lg font-medium flex items-center gap-1.5"
                style={{
                  backgroundColor: `${badgeColor}18`,
                  color: badgeColor,
                  border: `1px solid ${badgeColor}30`,
                }}
              >
                <BadgeIcon size={12} /> {label}
              </span>

              {approval.autonomy_level && (
                <Pill
                  color={AUTONOMY_COLORS[approval.autonomy_level] ?? C.textDim}
                  size="sm"
                >
                  {approval.autonomy_level}
                </Pill>
              )}

              {approval.confidence !== null && approval.confidence !== undefined && (
                <span className="text-[10px] text-[var(--color-text-muted)]">
                  {Math.round((approval.confidence) * 100)}% confidence
                </span>
              )}

              <span className="text-[10px] ml-auto text-[var(--color-text-muted)]">
                {timeAgo(approval.created_at)}
              </span>
            </div>

            <p className="text-sm mt-2.5 leading-relaxed text-[var(--color-text-primary)]">
              {approval.description}
            </p>

            {/* Blocker details */}
            {isBlocker && approval.payload && (() => {
              const p = approval.payload as {
                blocked_agent_name?: string;
                blocker_type?: string;
                description?: string;
                question?: string;
                project_name?: string;
                task_title?: string;
                blocker_comment?: string;
              };
              const blockerType = BLOCKER_TYPE_LABELS[p.blocker_type ?? "other"] ?? BLOCKER_TYPE_LABELS.other;

              return (
                <div className="mt-3 p-3 rounded-xl space-y-2" style={{ backgroundColor: `${C.error}0F`, border: `1px solid ${C.error}26` }}>
                  {(p.project_name || p.task_title) && (
                    <p className="text-[10px] text-[var(--color-text-muted)]">
                      {p.project_name && <span>{p.project_name}</span>}
                      {p.project_name && p.task_title && <span> · </span>}
                      {p.task_title && <span>{p.task_title}</span>}
                    </p>
                  )}
                  <span className="inline-block text-[10px] px-2 py-0.5 rounded font-medium" style={{ backgroundColor: `${blockerType.color}18`, color: blockerType.color, border: `1px solid ${blockerType.color}30` }}>
                    {blockerType.label}
                  </span>
                  {p.description && <p className="text-[12px] text-[var(--color-text-primary)]">{p.description}</p>}
                  {p.question && <p className="text-[12px] italic" style={{ color: C.accent }}>{p.question}</p>}
                  {!p.description && p.blocker_comment && (
                    <div className="prose-comment text-[11px] text-[var(--color-text-secondary)]">
                      <ReactMarkdown>{p.blocker_comment}</ReactMarkdown>
                    </div>
                  )}
                </div>
              );
            })()}

            {/* Clarification question */}
            {approval.action_type === "clarification_question" && approval.payload && (() => {
              const p = approval.payload as {
                asking_agent_name?: string;
                task_title?: string;
                project_name?: string;
                question?: string;
                options?: string[];
              };

              return (
                <div className="mt-3 p-3 rounded-xl space-y-2" style={{ backgroundColor: `${C.accent}0F`, border: `1px solid ${C.accent}26` }}>
                  {(p.project_name || p.task_title) && (
                    <p className="text-[10px] text-[var(--color-text-muted)]">
                      {p.project_name && <span>{p.project_name}</span>}
                      {p.project_name && p.task_title && <span> · </span>}
                      {p.task_title && <span>{p.task_title}</span>}
                    </p>
                  )}
                  <p className="text-[12px] text-[var(--color-text-primary)] font-medium">{p.question}</p>
                  {p.options && p.options.length > 0 && (
                    <div className="flex flex-wrap gap-1.5 mt-1">
                      {p.options.map((opt, i) => (
                        <button key={i} onClick={() => setNote(opt)} className="text-[11px] px-2.5 py-1 rounded-lg cursor-pointer transition-all" style={{ backgroundColor: note === opt ? `${C.accent}33` : "rgba(255,255,255,0.04)", color: note === opt ? C.accent : "var(--color-text-secondary)", border: `1px solid ${note === opt ? `${C.accent}66` : "rgba(255,255,255,0.08)"}` }}>
                          {opt}
                        </button>
                      ))}
                    </div>
                  )}
                </div>
              );
            })()}

            {/* Visual review screenshots */}
            {approval.action_type === "visual_review" && approval.payload && (() => {
              const payload = approval.payload as { screenshots?: string[]; preview_url?: string };
              return (
                <div className="mt-3 space-y-2">
                  <div className="flex gap-2 flex-wrap">
                    {payload.screenshots?.map((src, i) => (
                      <a
                        key={i}
                        href={src}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="block w-44 h-28 rounded-xl overflow-hidden transition-all hover:ring-2 hover:ring-[var(--color-accent)]/30"
                        style={{ border: "1px solid rgba(255,255,255,0.06)" }}
                      >
                        <img src={src} alt={`Screenshot ${i + 1}`} className="w-full h-full object-cover" />
                      </a>
                    ))}
                  </div>
                  {payload.preview_url && (
                    <a
                      href={payload.preview_url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="inline-flex items-center gap-1.5 text-sm transition-colors"
                      style={{ color: "var(--color-accent-light)" }}
                    >
                      <ExternalLink size={13} /> Preview oeffnen
                    </a>
                  )}
                </div>
              );
            })()}
          </div>
        </div>

        {/* Blocker / clarification note input */}
        {(isBlocker || approval.action_type === "clarification_question") && (
          <div className="mt-3">
            <textarea
              value={note}
              onChange={(e) => setNote(e.target.value)}
              placeholder="Anweisung fuer den Agent..."
              rows={2}
              className="w-full px-3 py-2.5 rounded-xl text-[12px] outline-none resize-none"
              style={{
                backgroundColor: "rgba(255,255,255,0.03)",
                color: "var(--color-text-primary)",
                border: "1px solid rgba(255,255,255,0.06)",
              }}
            />
          </div>
        )}

        {/* Actions */}
        <div
          className="flex items-center gap-2 mt-3 pt-3 border-t"
          style={{ borderColor: "rgba(255,255,255,0.06)" }}
        >
          <button
            onClick={() => onResolve("approved", note || undefined)}
            disabled={loading}
            className="flex items-center gap-1.5 text-[12px] px-3.5 py-2 rounded-xl cursor-pointer transition-all disabled:opacity-50"
            style={{
              backgroundColor: `${C.online}1F`,
              color: C.online,
              border: `1px solid ${C.online}40`,
            }}
          >
            <CheckCircle size={13} /> {approval.action_type === "clarification_question" ? "Antworten" : isBlocker ? "Entblocken" : "Approve"}
          </button>
          {approval.action_type !== "clarification_question" && (
            <button
              onClick={() => onResolve("rejected", note || undefined)}
              disabled={loading}
              className="flex items-center gap-1.5 text-[12px] px-3.5 py-2 rounded-xl cursor-pointer transition-all disabled:opacity-50"
              style={{
                backgroundColor: `${C.error}1F`,
                color: C.error,
                border: `1px solid ${C.error}40`,
              }}
            >
              <XCircle size={13} /> {isBlocker ? "Task abbrechen" : "Reject"}
            </button>
          )}
        </div>
      </GlassCard>
    </motion.div>
  );
}
