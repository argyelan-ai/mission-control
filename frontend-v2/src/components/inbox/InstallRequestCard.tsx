"use client";

import { useState } from "react";
import { motion } from "framer-motion";
import { Check, X, Package, Puzzle, Server } from "lucide-react";
import { cn, timeAgo } from "@/lib/utils";
import { GlassCard } from "@/components/shared/GlassCard";
import { Pill } from "@/components/shared/Pill";
import { api } from "@/lib/api";
import { C } from "@/lib/colors";
import type { Approval, InstallApprovalPayload, InstallActionType } from "@/lib/types";

// ── Types ────────────────────────────────────────────────────────────────────

interface Props {
  approval: Approval;
  onResolve: () => void;
}

// ── Helpers ───────────────────────────────────────────────────────────────────

const TYPE_ICON = {
  skill: Puzzle,
  plugin: Package,
  mcp: Server,
} as const;

const AUTONOMY_COLORS: Record<string, string> = {
  L1: C.online,
  L2: C.warning,
  L3: C.error,
};

function parseActionType(actionType: string): {
  operation: "install" | "uninstall";
  resourceType: "skill" | "plugin" | "mcp";
} {
  const parts = actionType.split("_");
  const operation = parts[0] as "install" | "uninstall";
  const resourceType = parts[1] as "skill" | "plugin" | "mcp";
  return { operation, resourceType };
}

// ── Component ─────────────────────────────────────────────────────────────────

export function InstallRequestCard({ approval, onResolve }: Props) {
  const payload = approval.payload as InstallApprovalPayload | null;
  const [isResolving, setIsResolving] = useState<"approve" | "reject" | null>(null);
  const [rejectReason, setRejectReason] = useState("");

  if (!payload || !payload.name) return null;

  const { operation, resourceType } = parseActionType(approval.action_type);
  const Icon = TYPE_ICON[resourceType] ?? Package;
  const verbDE = operation === "install" ? "Installieren" : "Deinstallieren";
  const accentColor = operation === "install" ? C.online : C.warning;

  async function resolve(status: "approved" | "rejected") {
    setIsResolving(status === "approved" ? "approve" : "reject");
    try {
      await api.approvals.resolve(
        approval.id,
        status,
        status === "rejected" && rejectReason ? rejectReason : undefined,
      );
      onResolve();
    } finally {
      setIsResolving(null);
    }
  }

  return (
    <motion.div
      layout
      initial={{ opacity: 0, x: -8 }}
      animate={{ opacity: 1, x: 0 }}
      exit={{ opacity: 0, x: 8, height: 0 }}
    >
      <GlassCard className="p-4" glow={`${accentColor}12`}>
        {/* Header */}
        <div className="flex items-start justify-between gap-4">
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2 flex-wrap">
              {/* Type badge */}
              <span
                className="text-[11px] px-2 py-0.5 rounded-lg font-medium flex items-center gap-1.5"
                style={{
                  backgroundColor: `${accentColor}18`,
                  color: accentColor,
                  border: `1px solid ${accentColor}30`,
                }}
              >
                <Icon size={12} />
                {verbDE} {resourceType}
              </span>

              {approval.autonomy_level && (
                <Pill
                  color={AUTONOMY_COLORS[approval.autonomy_level] ?? "#5A5E66"}
                  size="sm"
                >
                  {approval.autonomy_level}
                </Pill>
              )}

              <span className="text-[10px] ml-auto text-[var(--color-text-muted)]">
                {timeAgo(approval.created_at)}
              </span>
            </div>

            {/* Resource name */}
            <p className="text-sm mt-2.5 leading-relaxed text-[var(--color-text-primary)]">
              <span className="text-[var(--color-text-secondary)]">Boss schlaegt vor: </span>
              <span className="font-semibold">{verbDE}</span>
            </p>

            {/* Agents */}
            <p className="mt-1 text-xs text-[var(--color-text-muted)]">
              Target:{" "}
              <span className="font-mono text-[var(--color-text-secondary)]">
                {payload.target_agent_slug ?? payload.target_agent_id}
              </span>
              {" · "}
              Requester:{" "}
              <span className="font-mono text-[var(--color-text-secondary)]">
                {payload.requester_agent_slug ?? payload.requester_agent_id}
              </span>
            </p>
          </div>
        </div>

        {/* Name + Source */}
        <div
          className="mt-3 rounded-xl px-3 py-2.5"
          style={{
            backgroundColor: "rgba(255,255,255,0.03)",
            border: "1px solid rgba(255,255,255,0.06)",
          }}
        >
          <p className="text-[10px] text-[var(--color-text-muted)] mb-0.5">Paket</p>
          <code className="text-[12px] text-[var(--color-text-primary)] font-mono font-semibold break-all">
            {payload.source ? `${payload.name}  ·  ${payload.source}` : payload.name}
          </code>
        </div>

        {/* Reason */}
        <div
          className="mt-3 rounded-xl px-3 py-2.5"
          style={{
            backgroundColor: "rgba(255,255,255,0.03)",
            border: "1px solid rgba(255,255,255,0.06)",
          }}
        >
          <p className="text-[10px] text-[var(--color-text-muted)] mb-0.5">Begruendung</p>
          <p className="text-[12px] text-[var(--color-text-primary)] whitespace-pre-wrap leading-relaxed">
            {payload.reason}
          </p>
        </div>

        {/* Proposed config (optional) */}
        {payload.proposed_config && (
          <div
            className="mt-3 rounded-xl px-3 py-2.5"
            style={{
              backgroundColor: "rgba(255,255,255,0.03)",
              border: "1px solid rgba(255,255,255,0.06)",
            }}
          >
            <p className="text-[10px] text-[var(--color-text-muted)] mb-1">Proposed Config</p>
            <pre className="text-[11px] text-[var(--color-text-secondary)] overflow-x-auto">
              {JSON.stringify(payload.proposed_config, null, 2)}
            </pre>
          </div>
        )}

        {/* Actions */}
        <div
          className="flex items-center gap-2 mt-3 pt-3 border-t flex-wrap"
          style={{ borderColor: "rgba(255,255,255,0.06)" }}
        >
          <button
            onClick={() => resolve("approved")}
            disabled={isResolving !== null}
            className="flex items-center gap-1.5 text-[12px] px-3.5 py-2 rounded-xl cursor-pointer transition-all disabled:opacity-50"
            style={{
              backgroundColor: `${C.online}1F`,
              color: C.online,
              border: `1px solid ${C.online}40`,
            }}
          >
            <Check size={13} />
            {operation === "install" ? "Approve & install" : "Approve & uninstall"}
          </button>

          <button
            onClick={() => resolve("rejected")}
            disabled={isResolving !== null}
            className="flex items-center gap-1.5 text-[12px] px-3.5 py-2 rounded-xl cursor-pointer transition-all disabled:opacity-50"
            style={{
              backgroundColor: `${C.error}1F`,
              color: C.error,
              border: `1px solid ${C.error}40`,
            }}
          >
            <X size={13} />
            Ablehnen
          </button>

          <input
            type="text"
            placeholder="Ablehnungsgrund (optional)"
            value={rejectReason}
            onChange={(e) => setRejectReason(e.target.value)}
            className="flex-1 min-w-[180px] rounded-xl px-3 py-2 text-[11px] outline-none"
            style={{
              backgroundColor: "rgba(255,255,255,0.03)",
              color: "var(--color-text-primary)",
              border: "1px solid rgba(255,255,255,0.06)",
            }}
          />
        </div>
      </GlassCard>
    </motion.div>
  );
}
