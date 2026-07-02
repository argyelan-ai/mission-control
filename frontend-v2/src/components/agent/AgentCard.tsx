"use client";

import Link from "next/link";
import { motion } from "framer-motion";
import { useQuery } from "@tanstack/react-query";
import { BookOpen } from "lucide-react";
import { cn, contextPercent, contextColor, timeAgo } from "@/lib/utils";
import { api } from "@/lib/api";
import { C } from "@/lib/colors";
import { SpotlightCard } from "@/components/shared/SpotlightCard";
import { GlassCard } from "@/components/shared/GlassCard";
import { StatusDot } from "@/components/shared/StatusDot";
import { Pill } from "@/components/shared/Pill";
import { RuntimePill } from "@/components/shared/RuntimePill";
import type { Agent, SkillsResponse, OpenClawSkill } from "@/lib/types";

// ── Provision Badge ───────────────────────────────────────────────────────────

const PROVISION_CONFIG: Record<string, { label: string; color: string }> = {
  local: { label: "Local", color: C.textDim },
  provisioning: { label: "Provisioning", color: C.warning },
  provisioned: { label: "Live", color: C.online },
  error: { label: "Error", color: C.error },
};

function ProvisionBadge({ status }: { status: string }) {
  const cfg = PROVISION_CONFIG[status] ?? PROVISION_CONFIG.local;
  return (
    <Pill color={cfg.color} size="sm">
      {cfg.label}
    </Pill>
  );
}

// ── Skill Badges ──────────────────────────────────────────────────────────────

const MAX_VISIBLE_SKILLS = 3;

const SKILL_STATUS_COLORS: Record<string, string> = {
  ready: C.online,
  missing_bin: C.warning,
  missing_env: C.warning,
  disabled: C.textDim,
  not_installed: C.error,
};

export function SkillBadges({ skills }: { skills: string[] }) {
  const { data: skillsData } = useQuery<SkillsResponse>({
    queryKey: ["openclaw-skills"],
    queryFn: () => api.skills.list(),
    staleTime: 60_000,
  });

  const allSkills = skillsData?.skills ?? [];
  if (!skills.length) return null;

  const visible = skills.slice(0, MAX_VISIBLE_SKILLS);
  const remaining = skills.length - MAX_VISIBLE_SKILLS;

  const findSkill = (key: string): OpenClawSkill | undefined =>
    allSkills.find((s) => s.key === key || s.name === key);

  return (
    <div className="flex flex-wrap items-center gap-1">
      {visible.map((skillKey) => {
        const skill = findSkill(skillKey);
        const color = skill ? (SKILL_STATUS_COLORS[skill.status] ?? C.accent) : C.accent;
        const name = skill?.name ?? skillKey;
        return (
          <span
            key={skillKey}
            className="text-[10px] px-1.5 py-0.5 rounded-full leading-tight"
            style={{
              backgroundColor: `${color}22`,
              color,
              border: `1px solid ${color}33`,
            }}
          >
            {skill?.emoji && <span className="mr-0.5">{skill.emoji}</span>}
            {name}
          </span>
        );
      })}
      {remaining > 0 && (
        <span
          className="text-[10px] px-1.5 py-0.5 rounded-full"
          style={{ color: "var(--color-text-muted)", backgroundColor: "rgba(255,255,255,0.04)" }}
        >
          +{remaining}
        </span>
      )}
    </div>
  );
}

// ── Learning Badge ────────────────────────────────────────────────────────────

function LearningBadge({ agentId }: { agentId: string }) {
  const { data } = useQuery({
    queryKey: ["knowledge-stats", agentId],
    queryFn: () => api.knowledge.stats({ agent_id: agentId }),
    staleTime: 60_000,
  });

  const lessonCount = data?.stats?.lesson ?? 0;
  if (lessonCount === 0) return null;

  return (
    <span
      className="flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded-full"
      style={{ color: C.warning, backgroundColor: `${C.warning}1A` }}
    >
      <BookOpen size={10} />
      {lessonCount} {lessonCount === 1 ? "Lesson" : "Lessons"}
    </span>
  );
}

// ── Status Mapping ────────────────────────────────────────────────────────────

type DotStatus = "online" | "busy" | "idle" | "offline" | "error" | "warning";

function agentStatusToDot(status: string): DotStatus {
  switch (status) {
    case "online": return "online";
    case "busy": return "busy";
    case "error": return "error";
    case "restarting": return "warning";
    case "idle": return "idle";
    case "offline": return "offline";
    default: return "offline";
  }
}

// ── Agent Card ────────────────────────────────────────────────────────────────

interface AgentCardProps {
  agent: Agent;
  className?: string;
}

export function AgentCard({ agent, className }: AgentCardProps) {
  const pct = contextPercent(agent.context_tokens, agent.context_max);
  const barColor = contextColor(pct);
  const displaySkills = agent.skill_filter ?? agent.skills ?? [];
  const dotStatus = agentStatusToDot(agent.status);

  return (
    <Link href={`/agents/${agent.id}`} className="block">
      <SpotlightCard>
        <GlassCard
          className={cn(
            "p-4 hover:border-[rgba(255,255,255,0.12)] transition-all duration-200",
            className
          )}
        >
          <motion.div
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.3, ease: [0.16, 1, 0.3, 1] }}
          >
            {/* Top row: emoji + name + status */}
            <div className="flex items-start justify-between gap-3">
              <div className="flex items-center gap-3 min-w-0">
                <span className="text-2xl shrink-0">{agent.emoji ?? "🤖"}</span>
                <div className="min-w-0">
                  <h3 className="text-[16px] font-semibold text-[var(--color-text-primary)] truncate leading-tight">
                    {agent.name}
                  </h3>
                  {agent.role && (
                    <p className="text-[11px] text-[var(--color-text-muted)] truncate mt-0.5">
                      {agent.role}
                    </p>
                  )}
                </div>
              </div>

              <div className="flex items-center gap-2 shrink-0">
                <StatusDot
                  status={dotStatus}
                  size="md"
                  pulse={dotStatus === "online" || dotStatus === "busy"}
                />
              </div>
            </div>

            {/* Model + Runtime */}
            <div className="mt-3 flex items-center gap-2 flex-wrap">
              <span className="text-[11px] text-[var(--color-text-muted)] truncate font-mono">
                {agent.model ? agent.model.split("/").pop() : "No model"}
              </span>
              <RuntimePill agent={agent} variant="compact" />
            </div>

            {/* Context token bar */}
            <div className="mt-3">
              <div className="flex items-center justify-between mb-1">
                <span className="text-[10px] text-[var(--color-text-muted)]">Context</span>
                <span className="text-[10px] text-[var(--color-text-muted)]">{pct}%</span>
              </div>
              <div className="h-1 rounded-full bg-[rgba(255,255,255,0.06)] overflow-hidden">
                <motion.div
                  className="h-full rounded-full"
                  style={{ backgroundColor: barColor }}
                  initial={{ width: 0 }}
                  animate={{ width: `${Math.min(pct, 100)}%` }}
                  transition={{ duration: 0.6, ease: [0.16, 1, 0.3, 1] }}
                />
              </div>
            </div>

            {/* Skills + Learning */}
            <div className="mt-3 flex items-center gap-2 flex-wrap">
              <SkillBadges skills={displaySkills} />
              <LearningBadge agentId={agent.id} />
            </div>

            {/* Footer: last seen + provision */}
            <div className="mt-3 flex items-center justify-between">
              <span className="text-[10px] text-[var(--color-text-muted)]">
                {timeAgo(agent.last_seen_at)}
              </span>
              <ProvisionBadge status={agent.provision_status} />
            </div>
          </motion.div>
        </GlassCard>
      </SpotlightCard>
    </Link>
  );
}
