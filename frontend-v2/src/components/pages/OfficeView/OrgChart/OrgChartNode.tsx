"use client";

import { motion } from "framer-motion";
import { useTranslations } from "next-intl";
import { Container, HardDrive, Radio, UserRound } from "lucide-react";
import type { OrgNode, OrgRuntime, OrgStatus } from "./types";
import { C, STATUS as STATUS_TOKENS } from "@/lib/colors";

// ── Status palette ────────────────────────────────────────────────────────

const STATUS: Record<OrgStatus, { color: string; label: string; pulse: boolean }> = {
  online:  { color: C.online,          label: "online",  pulse: false },
  working: { color: C.accent,          label: "working", pulse: true  },
  offline: { color: STATUS_TOKENS.offline, label: "offline", pulse: false },
  warning: { color: C.warning,         label: "warning", pulse: true  },
  error:   { color: C.error,           label: "error",   pulse: true  },
};

// ── Runtime badge config ──────────────────────────────────────────────────

const RUNTIME: Record<OrgRuntime, { label: string; icon: typeof HardDrive; color: string }> = {
  human:  { label: "human",  icon: UserRound, color: C.textPrimary },
  voice:  { label: "voice",  icon: Radio,     color: C.accent },
  host:   { label: "host",   icon: HardDrive, color: C.warning },
  docker: { label: "docker", icon: Container, color: C.textSecondary },
};

// ── Card props ────────────────────────────────────────────────────────────

interface OrgChartNodeProps {
  node: OrgNode;
}

export function OrgChartNode({ node }: OrgChartNodeProps) {
  const t = useTranslations("office");
  const status = STATUS[node.status];
  const runtime = RUNTIME[node.runtime];
  const Icon = node.icon;
  const RuntimeIcon = runtime.icon;

  // ── Variant: Operator (the operator) — the human, top of the tree ──────
  if (node.tier === "operator") {
    return (
      <div
        data-node-id={node.id}
        className="org-node org-node--operator group relative w-[340px] rounded-md px-6 py-5"
        style={{
          background:
            `linear-gradient(155deg, ${C.bgElevated} 0%, ${C.bgSurface} 60%)`,
          border: `1px solid ${C.borderActive}`,
          boxShadow: "0 18px 40px -20px rgba(0,0,0,0.6)",
        }}
      >
        {/* light streak on top edge — signals "human, source of truth" */}
        <div
          aria-hidden
          className="absolute inset-x-6 top-0 h-px"
          style={{
            background:
              `linear-gradient(90deg, transparent, ${C.borderActive}, transparent)`,
          }}
        />

        <div className="flex items-center gap-4">
          <div
            className="grid place-items-center rounded-full"
            style={{
              width: 56, height: 56,
              background: "radial-gradient(circle at 30% 25%, #fafafa, #d4d4d8 60%, #71717a)",
              boxShadow: "0 6px 14px rgba(0,0,0,0.4), inset 0 1px 0 rgba(255,255,255,0.5)",
              color: "#0a0a0a",
            }}
          >
            <Icon size={26} strokeWidth={2} />
          </div>

          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <span className="text-[10px] uppercase tracking-[0.2em] font-mono font-medium">
                {t("tierOperator")}
              </span>
              <span className="h-px flex-1" style={{ background: C.borderSubtle }} />
              <StatusDot status={status} />
            </div>
            <div className="display text-[24px] font-semibold leading-tight mt-1 tracking-tight" style={{ color: C.textPrimary }}>
              {node.name}
            </div>
            <div className="text-[13px] mt-1.5 leading-snug" style={{ color: C.textSecondary }}>
              {t(node.taglineKey)}
            </div>
          </div>
        </div>
      </div>
    );
  }

  // ── Variant: Voice (Jarvis) — in-line between the operator and Boss ────
  if (node.tier === "voice") {
    return (
      <div
        data-node-id={node.id}
        className="org-node org-node--voice group relative w-[300px] rounded-md px-5 py-4"
        style={{
          background:
            `linear-gradient(160deg, ${C.accentSubtle} 0%, ${C.bgSurface} 65%)`,
          border: `1px solid ${C.borderAccent}`,
          boxShadow: "0 14px 32px -22px rgba(0,0,0,0.5)",
        }}
      >
        <div className="flex items-start gap-4">
          <div
            className="relative grid place-items-center rounded-xl shrink-0"
            style={{
              width: 52, height: 52,
              background:
                `linear-gradient(135deg, ${C.accentSubtle}, ${C.bgSurface})`,
              border: `1px solid ${C.borderAccent}`,
              color: C.accentHover,
            }}
          >
            <Icon size={24} strokeWidth={2.2} />
            {/* breathing pulse ring — voice is always "listening" */}
            <motion.div
              aria-hidden
              className="absolute inset-0 rounded-xl pointer-events-none"
              animate={{ boxShadow: [`0 0 0 0 ${C.accent}66`, `0 0 0 12px ${C.accent}00`] }}
              transition={{ duration: 2.4, repeat: Infinity, ease: "easeOut" }}
            />
          </div>

          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <span className="text-[10px] uppercase tracking-[0.18em] font-medium" style={{ color: `${C.accent}B3` }}>
                {t("tierVoice")}
              </span>
              <span className="h-px flex-1" style={{ background: C.borderSubtle }} />
              <StatusDot status={status} />
            </div>
            <div className="display text-[20px] font-semibold mt-1 leading-tight" style={{ color: C.textPrimary }}>
              {node.name}
            </div>
            <RuntimeRow runtime={runtime} RuntimeIcon={RuntimeIcon} model={node.model} />
            <div className="text-[12px] mt-2 leading-snug" style={{ color: C.textSecondary }}>
              {t(node.taglineKey)}
            </div>
          </div>
        </div>
      </div>
    );
  }

  // ── Variant: Lead (Boss) — central authority card ──────────────────────
  if (node.tier === "lead") {
    return (
      <div
        data-node-id={node.id}
        className="org-node org-node--lead group relative w-[380px] rounded-md px-6 py-5"
        style={{
          background:
            `linear-gradient(135deg, ${C.accentSubtle} 0%, ${C.bgSurface} 55%, ${C.bgSurface} 100%)`,
          border: `1px solid ${C.borderAccent}`,
          boxShadow:
            "0 22px 52px -26px rgba(0,0,0,0.6), inset 0 1px 0 rgba(255,255,255,0.04)",
        }}
      >
        {/* radial accent — communicates "centre of gravity" */}
        <div
          aria-hidden
          className="pointer-events-none absolute inset-0 rounded-md"
          style={{
            background:
              `radial-gradient(60% 80% at 50% 0%, ${C.accentSubtle}, transparent 70%)`,
          }}
        />

        <div className="relative flex items-start gap-4">
          <div
            className="grid place-items-center rounded-xl shrink-0"
            style={{
              width: 60, height: 60,
              background:
                `linear-gradient(140deg, ${C.accent} 0%, ${C.accentHover} 100%)`,
              boxShadow: "0 8px 22px -6px rgba(0,0,0,0.5)",
              color: C.onAccent,
            }}
          >
            <Icon size={28} strokeWidth={2} />
          </div>

          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <span className="text-[10px] uppercase tracking-[0.2em] font-semibold" style={{ color: C.accent }}>
                {t("roleLeadOrchestrator")}
              </span>
              <span className="h-px flex-1" style={{ background: C.borderAccent }} />
              <StatusDot status={status} />
            </div>
            <div className="display text-[26px] font-semibold leading-tight mt-1 tracking-tight" style={{ color: C.textPrimary }}>
              {node.name}
            </div>
            <RuntimeRow runtime={runtime} RuntimeIcon={RuntimeIcon} model={node.model} />
            <div className="text-[13px] mt-2 leading-snug" style={{ color: C.textSecondary }}>
              {t(node.taglineKey)}
            </div>
          </div>
        </div>
      </div>
    );
  }

  // ── Variant: Worker — bottom row cards ─────────────────────────────────
  return (
    <div
      data-node-id={node.id}
      className="org-node org-node--worker group relative w-[208px] rounded-md px-4 py-3.5 transition-colors"
      style={{
        background: C.bgSurface,
        border: `1px solid ${C.border}`,
      }}
    >
      <div className="flex items-center gap-3">
        <div
          className="grid place-items-center rounded-lg shrink-0"
          style={{
            width: 38, height: 38,
            background: `linear-gradient(140deg, ${status.color}26, ${C.bgSurface})`,
            border: `1px solid ${status.color}30`,
            color: status.color,
          }}
        >
          <Icon size={19} strokeWidth={2.1} />
        </div>
        <div className="min-w-0 flex-1">
          <div className="text-[15px] font-semibold leading-tight truncate">
            {node.name}
          </div>
          <div className="text-[10.5px] uppercase tracking-[0.1em] mt-0.5 font-mono">
            {t(node.roleKey)}
          </div>
        </div>
        <StatusDot status={status} compact />
      </div>

      <div className="mt-3 flex items-center gap-1.5 text-[11px]">
        <RuntimeIcon size={12} strokeWidth={2} style={{ color: runtime.color }} />
        <span className="font-mono uppercase tracking-wide">{runtime.label}</span>
        {node.model && (
          <>
            <span className="text-zinc-700">·</span>
            <span className="font-mono text-zinc-500 truncate">{node.model}</span>
          </>
        )}
      </div>

      <div className="mt-2 text-[11.5px] leading-snug line-clamp-2">
        {t(node.taglineKey)}
      </div>
    </div>
  );
}

// ── Sub-components ────────────────────────────────────────────────────────

function StatusDot({
  status,
  compact = false,
}: {
  status: { color: string; label: string; pulse: boolean };
  compact?: boolean;
}) {
  const size = compact ? 7 : 8;
  return (
    <span className="relative inline-flex items-center gap-1.5" aria-label={status.label}>
      <span
        className="relative inline-block rounded-full"
        style={{ width: size, height: size, background: status.color }}
      >
        {status.pulse && (
          <motion.span
            aria-hidden
            className="absolute inset-0 rounded-full"
            style={{ background: status.color }}
            animate={{ scale: [1, 2.4], opacity: [0.6, 0] }}
            transition={{ duration: 1.8, repeat: Infinity, ease: "easeOut" }}
          />
        )}
      </span>
      {!compact && (
        <span className="text-[9px] text-zinc-500 uppercase tracking-[0.1em] font-medium">
          {status.label}
        </span>
      )}
    </span>
  );
}

function RuntimeRow({
  runtime,
  RuntimeIcon,
  model,
}: {
  runtime: { label: string; color: string };
  RuntimeIcon: typeof HardDrive;
  model?: string;
}) {
  return (
    <div className="flex items-center gap-1.5 text-[10.5px] text-zinc-500 mt-1.5">
      <span
        className="inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 font-mono uppercase tracking-wider"
        style={{
          background: `${runtime.color}12`,
          border: `1px solid ${runtime.color}22`,
          color: runtime.color,
          fontSize: 9.5,
        }}
      >
        <RuntimeIcon size={10} strokeWidth={2.2} />
        {runtime.label}
      </span>
      {model && (
        <span className="font-mono text-zinc-500 truncate">{model}</span>
      )}
    </div>
  );
}
