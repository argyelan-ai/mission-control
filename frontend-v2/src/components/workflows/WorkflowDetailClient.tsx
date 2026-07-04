"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowLeft,
  ChevronDown,
  ChevronUp,
  Clock3,
  Copy,
  History,
  Pause,
  Play,
  Plus,
  RefreshCw,
  Save,
  Send,
  Square,
  Trash2,
} from "lucide-react";
import AppShell from "@/components/layout/AppShell";
import { GlassCard } from "@/components/shared/GlassCard";
import { api, sseUrls } from "@/lib/api";
import { notify } from "@/lib/notify";
import { useSSE } from "@/lib/sse";
import type {
  Agent,
  WorkflowRun,
  WorkflowStepDefinition,
  WorkflowStepRun,
  WorkflowTemplate,
} from "@/lib/types";
import { cn, timeAgo } from "@/lib/utils";
import { C } from "@/lib/colors";
import { formatWorkflowTriggerLabel, selectAiNewsAgents, WEEKDAY_OPTIONS } from "./presets";

const WORKFLOW_STATUS_META: Record<
  WorkflowTemplate["status"],
  { label: string; text: string; border: string; bg: string }
> = {
  draft: {
    label: "Draft",
    text: "var(--color-text-muted)",
    border: "rgba(255,255,255,0.08)",
    bg: "rgba(255,255,255,0.03)",
  },
  validated: {
    label: "Validated",
    text: C.accent,
    border: `${C.accent}4D`,
    bg: `${C.accent}1A`,
  },
  active: {
    label: "Active",
    text: "var(--color-status-online)",
    border: "rgba(0,204,136,0.28)",
    bg: "rgba(0,204,136,0.08)",
  },
  archived: {
    label: "Archived",
    text: "var(--color-text-muted)",
    border: "rgba(255,255,255,0.08)",
    bg: "rgba(255,255,255,0.02)",
  },
};

const RUN_STATUS_META: Record<
  WorkflowRun["status"],
  { label: string; text: string; border: string; bg: string }
> = {
  running: {
    label: "Running",
    text: C.accent,
    border: `${C.accent}59`,
    bg: `${C.accent}1A`,
  },
  paused: {
    label: "Paused",
    text: C.warning,
    border: "rgba(184,135,10,0.35)",
    bg: "rgba(184,135,10,0.1)",
  },
  completed: {
    label: "Completed",
    text: "var(--color-status-online)",
    border: "rgba(0,204,136,0.28)",
    bg: "rgba(0,204,136,0.08)",
  },
  partial: {
    label: "Partial",
    text: C.warning,
    border: "rgba(184,135,10,0.35)",
    bg: "rgba(184,135,10,0.1)",
  },
  failed: {
    label: "Failed",
    text: "var(--color-status-error)",
    border: "rgba(239,68,68,0.35)",
    bg: "rgba(239,68,68,0.1)",
  },
  stopped: {
    label: "Stopped",
    text: "var(--color-text-muted)",
    border: "rgba(255,255,255,0.08)",
    bg: "rgba(255,255,255,0.03)",
  },
  force_stopped: {
    label: "Force stopped",
    text: "var(--color-status-error)",
    border: "rgba(239,68,68,0.35)",
    bg: "rgba(239,68,68,0.1)",
  },
};

const STEP_STATUS_META: Record<
  WorkflowStepRun["status"],
  { label: string; text: string; border: string; bg: string }
> = {
  pending: {
    label: "Pending",
    text: "var(--color-text-muted)",
    border: "rgba(255,255,255,0.08)",
    bg: "rgba(255,255,255,0.03)",
  },
  running: {
    label: "Running",
    text: C.accent,
    border: `${C.accent}59`,
    bg: `${C.accent}1A`,
  },
  done: {
    label: "Done",
    text: "var(--color-status-online)",
    border: "rgba(0,204,136,0.28)",
    bg: "rgba(0,204,136,0.08)",
  },
  skipped: {
    label: "Skipped",
    text: C.warning,
    border: "rgba(184,135,10,0.35)",
    bg: "rgba(184,135,10,0.1)",
  },
  failed: {
    label: "Failed",
    text: "var(--color-status-error)",
    border: "rgba(239,68,68,0.35)",
    bg: "rgba(239,68,68,0.1)",
  },
  interrupted: {
    label: "Interrupted",
    text: "var(--color-text-muted)",
    border: "rgba(255,255,255,0.08)",
    bg: "rgba(255,255,255,0.03)",
  },
};

interface WorkflowDraftStep {
  key: string;
  name: string;
  step_type: "llm" | "deterministic" | "local";
  execution_mode: "single" | "swarm";
  input_template: string;
  timeout_seconds: number;
  on_error: "abort" | "retry" | "skip";
  retry_max_attempts: number;
  retry_delay_seconds: number;
  retry_backoff: "linear" | "exponential";
  output_type: "text" | "json";
  executor_type: string | null;
  agent_id: string;
  skill_key: string;
  executor_config_text: string;
  evaluation_contract_text: string;
}

interface WorkflowDraft {
  id: string;
  name: string;
  description: string;
  board_id: string;
  project_id: string;
  trigger_type: WorkflowTemplate["trigger_type"];
  trigger_config: Record<string, unknown>;
  status: WorkflowTemplate["status"];
  max_runtime_minutes: number;
  policy_profile: string;
  reflect_on: string;
  delivery_mode: "none" | "discord_channel";
  delivery_channel_id: string;
  delivery_channel_name: string;
  deliver_on: "success" | "failure" | "always";
  delivery_format: "summary_card" | "markdown";
  current_definition: { steps: WorkflowDraftStep[] };
  execution_policy: Record<string, unknown> | null;
}

interface AiNewsGuidedConfig {
  agent_id: string;
  topic_focus: string;
  custom_instructions: string;
  timeframe_hours: number;
  max_items: number;
  source_profile: "official" | "balanced" | "broad";
  fact_check_level: "fast" | "balanced" | "strict";
  include_impacts: boolean;
  include_emojis: boolean;
  include_openclaw_corner: boolean;
  openclaw_items: number;
}

type WorkflowEditorTab = "builder" | "runs";
type WorkflowEditorMode = "simple" | "advanced";
type StepPresetKind = "llm" | "internal_api" | "webhook" | "script_ref";
const SIMPLE_GOAL_MARKER = "## Benutzerziel";
const AI_NEWS_KIND = "ai_news_briefing";

function formatError(error: unknown): string {
  if (!(error instanceof Error)) return "Unknown error";
  const raw = error.message.replace(/^API \d+:\s*/, "");
  try {
    const parsed = JSON.parse(raw) as { detail?: string };
    return parsed.detail ?? raw;
  } catch {
    return raw;
  }
}

function formatJson(value: unknown): string {
  if (value == null) return "";
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return "";
  }
}

function parseObjectJson(
  text: string,
  label: string,
  options: {
    allowEmpty?: boolean;
    defaultWhenEmpty?: Record<string, unknown> | null;
  } = {},
): Record<string, unknown> | null {
  const { allowEmpty = true, defaultWhenEmpty = null } = options;
  const trimmed = text.trim();
  if (!trimmed) {
    return allowEmpty ? defaultWhenEmpty : null;
  }

  let parsed: unknown;
  try {
    parsed = JSON.parse(trimmed);
  } catch {
    throw new Error(`${label} must be valid JSON`);
  }

  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error(`${label} must be a JSON object`);
  }
  return parsed as Record<string, unknown>;
}

function defaultExecutorConfig(executorType: string | null): Record<string, unknown> {
  if (executorType === "webhook") {
    return {
      url: "https://example.com/webhook",
      method: "POST",
      headers: {
        Authorization: "Bearer {{secrets.MY_WEBHOOK_TOKEN}}",
      },
    };
  }
  if (executorType === "script_ref") {
    return {
      script: "sample_workflow.py",
      args: ["--mode", "summary"],
    };
  }
  return {
    path: "/api/v1/system/status",
    method: "GET",
  };
}

function toDraftStep(step: WorkflowStepDefinition): WorkflowDraftStep {
  const executorType =
    step.step_type === "deterministic"
      ? step.executor_type ?? "internal_api"
      : step.executor_type ?? null;

  return {
    key: step.key,
    name: step.name,
    step_type: step.step_type,
    execution_mode: step.execution_mode ?? "single",
    input_template: step.input_template ?? "",
    timeout_seconds: step.timeout_seconds ?? 300,
    on_error: step.on_error ?? "abort",
    retry_max_attempts: step.retry_max_attempts ?? 0,
    retry_delay_seconds: step.retry_delay_seconds ?? 0,
    retry_backoff: step.retry_backoff ?? "linear",
    output_type: step.output_type ?? "text",
    executor_type: executorType,
    agent_id: step.agent_id ?? "",
    skill_key: step.skill_key ?? "",
    executor_config_text: formatJson(
      step.executor_config ??
        (step.step_type === "deterministic" ? defaultExecutorConfig(executorType) : null)
    ),
    evaluation_contract_text: formatJson(step.evaluation_contract ?? null),
  };
}

function toWorkflowDraft(workflow: WorkflowTemplate): WorkflowDraft {
  const delivery = workflow.delivery_config ?? {};
  const deliveryMode =
    delivery.delivery_mode === "discord_channel" ? "discord_channel" : "none";
  const deliverOn =
    delivery.deliver_on === "failure" || delivery.deliver_on === "always"
      ? delivery.deliver_on
      : "success";
  const deliveryFormat =
    delivery.delivery_format === "markdown" ? "markdown" : "summary_card";

  return {
    id: workflow.id,
    name: workflow.name,
    description: workflow.description ?? "",
    board_id: workflow.board_id ?? "",
    project_id: workflow.project_id ?? "",
    trigger_type: workflow.trigger_type,
    trigger_config: { ...(workflow.trigger_config ?? {}) },
    status: workflow.status,
    max_runtime_minutes: workflow.max_runtime_minutes,
    policy_profile: workflow.policy_profile,
    reflect_on: workflow.reflect_on,
    delivery_mode: deliveryMode,
    // Phase 31 / OCS-15: delivery.gateway_id no longer read — Discord guild
    // is a singleton (settings.discord_guild_id); per-workflow gateway selection
    // removed. Legacy field in delivery payload is ignored on read.
    delivery_channel_id: String(delivery.channel_id ?? ""),
    delivery_channel_name: String(delivery.channel_name ?? ""),
    deliver_on: deliverOn,
    delivery_format: deliveryFormat,
    current_definition: {
      steps: (workflow.current_definition?.steps ?? []).map(toDraftStep),
    },
    execution_policy: workflow.execution_policy ?? null,
  };
}

function createStepTemplate(
  index: number,
  agents: Agent[],
  preset: StepPresetKind = "llm"
): WorkflowDraftStep {
  // Phase 31 / OCS-15: provisioned agents preferred over arbitrary first agent.
  const fallbackAgent =
    agents.find((agent) => agent.provision_status === "provisioned")?.id ??
    agents[0]?.id ??
    "";

  if (preset !== "llm") {
    const executorType =
      preset === "webhook"
        ? "webhook"
        : preset === "script_ref"
          ? "script_ref"
          : "internal_api";
    const label =
      executorType === "webhook"
        ? "Webhook"
        : executorType === "script_ref"
          ? "Script"
          : "API";
    return {
      key: `step_${index}`,
      name: `${label} Step ${index}`,
      step_type: "deterministic",
      execution_mode: "single",
      input_template: "",
      timeout_seconds: 300,
      on_error: "abort",
      retry_max_attempts: 0,
      retry_delay_seconds: 0,
      retry_backoff: "linear",
      output_type: "text",
      executor_type: executorType,
      agent_id: "",
      skill_key: "",
      executor_config_text: formatJson(defaultExecutorConfig(executorType)),
      evaluation_contract_text: "",
    };
  }

  return {
    key: `step_${index}`,
    name: `Step ${index}`,
    step_type: "llm",
    execution_mode: "single",
    input_template: "",
    timeout_seconds: 300,
    on_error: "abort",
    retry_max_attempts: 0,
    retry_delay_seconds: 0,
    retry_backoff: "linear",
    output_type: "text",
    executor_type: null,
    agent_id: fallbackAgent,
    skill_key: "",
    executor_config_text: "",
    evaluation_contract_text: "",
  };
}

function stepPresetLabel(preset: StepPresetKind): string {
  switch (preset) {
    case "llm":
      return "LLM";
    case "internal_api":
      return "API";
    case "webhook":
      return "Webhook";
    case "script_ref":
      return "Script";
  }
}

function workflowKind(policy: Record<string, unknown> | null | undefined): string | null {
  const value = policy?.workflow_kind;
  if (typeof value !== "string" || !value.trim()) return null;
  return value.trim();
}

function defaultAiNewsConfig(agentId = ""): AiNewsGuidedConfig {
  return {
    agent_id: agentId,
    topic_focus: "",
    custom_instructions:
      "Find the 5-7 most important AI news items from the last 24 hours. Use Discord-friendly markdown, prefer primary sources, and include an OpenClaw Corner.",
    timeframe_hours: 24,
    max_items: 7,
    source_profile: "balanced",
    fact_check_level: "strict",
    include_impacts: true,
    include_emojis: true,
    include_openclaw_corner: true,
    openclaw_items: 2,
  };
}

function readAiNewsConfig(
  policy: Record<string, unknown> | null | undefined,
  fallbackAgentId = ""
): AiNewsGuidedConfig {
  const raw =
    policy && typeof policy.guided_config === "object" && !Array.isArray(policy.guided_config)
      ? (policy.guided_config as Record<string, unknown>)
      : {};
  const defaults = defaultAiNewsConfig(fallbackAgentId);
  return {
    agent_id: String(raw.agent_id ?? defaults.agent_id),
    topic_focus: String(raw.topic_focus ?? defaults.topic_focus),
    custom_instructions: String(raw.custom_instructions ?? defaults.custom_instructions),
    timeframe_hours: Number(raw.timeframe_hours ?? defaults.timeframe_hours) || defaults.timeframe_hours,
    max_items: Number(raw.max_items ?? defaults.max_items) || defaults.max_items,
    source_profile:
      raw.source_profile === "official" || raw.source_profile === "broad"
        ? raw.source_profile
        : defaults.source_profile,
    fact_check_level:
      raw.fact_check_level === "fast" || raw.fact_check_level === "balanced"
        ? raw.fact_check_level
        : defaults.fact_check_level,
    include_impacts:
      typeof raw.include_impacts === "boolean"
        ? raw.include_impacts
        : defaults.include_impacts,
    include_emojis:
      typeof raw.include_emojis === "boolean"
        ? raw.include_emojis
        : defaults.include_emojis,
    include_openclaw_corner:
      typeof raw.include_openclaw_corner === "boolean"
        ? raw.include_openclaw_corner
        : defaults.include_openclaw_corner,
    openclaw_items: Number(raw.openclaw_items ?? defaults.openclaw_items) || defaults.openclaw_items,
  };
}

function writeAiNewsConfig(
  policy: Record<string, unknown> | null | undefined,
  config: AiNewsGuidedConfig
): Record<string, unknown> {
  return {
    ...(policy ?? {}),
    workflow_kind: AI_NEWS_KIND,
    guided_config: {
      ...config,
      timeframe_hours: Math.max(6, Math.min(168, Math.round(config.timeframe_hours))),
      max_items: Math.max(3, Math.min(10, Math.round(config.max_items))),
      openclaw_items: Math.max(1, Math.min(3, Math.round(config.openclaw_items))),
    },
  };
}

function findPrimaryLlmStepIndex(steps: WorkflowDraftStep[]): number {
  for (let index = steps.length - 1; index >= 0; index -= 1) {
    if (steps[index]?.step_type === "llm") return index;
  }
  return -1;
}

function extractSimpleGoal(template: string | null | undefined): string {
  const raw = template ?? "";
  const marker = `${SIMPLE_GOAL_MARKER}\n`;
  const markerIndex = raw.lastIndexOf(marker);
  if (markerIndex === -1) return "";
  return raw.slice(markerIndex + marker.length).trim();
}

function applySimpleGoal(template: string | null | undefined, goal: string): string {
  const raw = template ?? "";
  const marker = `${SIMPLE_GOAL_MARKER}\n`;
  const markerIndex = raw.lastIndexOf(marker);
  const base = (markerIndex === -1 ? raw : raw.slice(0, markerIndex)).trimEnd();
  const nextGoal = goal.trim();
  if (!nextGoal) return base;
  return `${base}${base ? "\n\n" : ""}${SIMPLE_GOAL_MARKER}\n${nextGoal}`;
}

function describeSimpleFlow(step: WorkflowDraftStep, agents: Agent[]): string {
  if (step.step_type === "llm") {
    const agent = agents.find((item) => item.id === step.agent_id);
    return agent
      ? `Writes the result with ${agent.name}`
      : "Writes the result with the selected agent";
  }
  if (step.executor_type === "webhook") {
    return "Calls an external webhook";
  }
  if (step.executor_type === "script_ref") {
    return "Runs an internal workflow script";
  }
  return "Collects or transforms internal data";
}

function buildStepKeyCandidate(value: string, fallback: string): string {
  const slug = value
    .toLowerCase()
    .trim()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "");
  return slug || fallback;
}

function describeStep(step: WorkflowDraftStep, agents: Agent[]): string {
  if (step.step_type === "llm") {
    const agent = agents.find((item) => item.id === step.agent_id);
    return agent ? `Agent: ${agent.name}` : "Agent not selected yet";
  }
  const executor = step.executor_type ?? "internal_api";
  return `Executor: ${executor}`;
}

function formatDateTime(value: string | null): string {
  if (!value) return "—";
  return new Date(value).toLocaleString("de-CH", {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function buildWorkflowPayload(
  draft: WorkflowDraft,
  {
    channelNameFallback,
    changeReason,
  }: {
    channelNameFallback: string | null;
    changeReason: string;
  },
) {
  if (!draft.name.trim()) {
    throw new Error("Workflow name is missing");
  }

  let triggerConfig: Record<string, unknown> | null = null;
  if (draft.trigger_type === "scheduled") {
    const scheduleType =
      draft.trigger_config.schedule_type === "interval"
        ? "interval"
        : draft.trigger_config.schedule_type === "weekly"
          ? "weekly"
        : draft.trigger_config.schedule_type === "weekdays"
          ? "weekdays"
          : "daily";
    if (scheduleType === "interval") {
      const hours = Number(draft.trigger_config.schedule_interval_hours ?? 0);
      if (!Number.isFinite(hours) || hours <= 0) {
        throw new Error("Interval trigger needs an hour value > 0");
      }
      triggerConfig = {
        schedule_type: "interval",
        schedule_interval_hours: Math.round(hours),
      };
    } else if (scheduleType === "weekly") {
      const scheduleTime = String(draft.trigger_config.schedule_time ?? "").trim();
      const scheduleDay = String(draft.trigger_config.schedule_day ?? "").trim().toLowerCase();
      if (!scheduleTime) {
        throw new Error("Weekly trigger needs a time");
      }
      if (!scheduleDay) {
        throw new Error("Weekly trigger needs a weekday");
      }
      triggerConfig = {
        schedule_type: "weekly",
        schedule_day: scheduleDay,
        schedule_time: scheduleTime,
      };
    } else {
      const scheduleTime = String(draft.trigger_config.schedule_time ?? "").trim();
      if (!scheduleTime) {
        throw new Error("Scheduled workflows need a time");
      }
      triggerConfig = {
        schedule_type: scheduleType,
        schedule_time: scheduleTime,
      };
    }
  }

  const draftWorkflowKind = workflowKind(draft.execution_policy);

  const steps = draft.current_definition.steps.map((step, index) => {
    if (!step.key.trim()) {
      throw new Error(`Step ${index + 1} needs a key`);
    }
    if (!step.name.trim()) {
      throw new Error(`Step ${index + 1} needs a name`);
    }

    const evaluationContract = parseObjectJson(
      step.evaluation_contract_text,
      `Evaluation contract for ${step.name.trim() || step.key.trim()}`,
      { allowEmpty: true, defaultWhenEmpty: null }
    );

    if (step.step_type === "llm") {
      if (!step.agent_id) {
        throw new Error(`LLM step "${step.name}" needs an agent`);
      }
      return {
        key: step.key.trim(),
        name: step.name.trim(),
        step_type: "llm" as const,
        execution_mode: "single" as const,
        input_template: step.input_template.trim() || null,
        timeout_seconds: Number(step.timeout_seconds) || 300,
        on_error: step.on_error,
        retry_max_attempts: Number(step.retry_max_attempts) || 0,
        retry_delay_seconds: Number(step.retry_delay_seconds) || 0,
        retry_backoff: step.retry_backoff,
        output_type: step.output_type,
        agent_id: step.agent_id,
        skill_key: step.skill_key.trim() || null,
        executor_type: null,
        executor_config: null,
        evaluation_contract: evaluationContract,
      };
    }

    if (step.step_type === "deterministic") {
      if (!step.executor_type) {
        throw new Error(`Deterministic step "${step.name}" needs an executor type`);
      }
      const executorConfig = parseObjectJson(
        step.executor_config_text,
        `Executor config for ${step.name.trim() || step.key.trim()}`,
        {
          allowEmpty: false,
          defaultWhenEmpty: null,
        }
      );
      if (!executorConfig) {
        throw new Error(`Executor config missing for ${step.name.trim() || step.key.trim()}`);
      }
      return {
        key: step.key.trim(),
        name: step.name.trim(),
        step_type: "deterministic" as const,
        execution_mode: "single" as const,
        input_template: step.input_template.trim() || null,
        timeout_seconds: Number(step.timeout_seconds) || 300,
        on_error: step.on_error,
        retry_max_attempts: Number(step.retry_max_attempts) || 0,
        retry_delay_seconds: Number(step.retry_delay_seconds) || 0,
        retry_backoff: step.retry_backoff,
        output_type: step.output_type,
        executor_type: step.executor_type,
        executor_config: executorConfig,
        agent_id: null,
        skill_key: null,
        evaluation_contract: evaluationContract,
      };
    }

      throw new Error(`Step type "${step.step_type}" is not enabled in the MVP yet`);
  });

  let deliveryConfig: Record<string, unknown> | null = null;
  if (draft.delivery_mode === "discord_channel") {
    // Phase 31 / OCS-15: gateway_id validation + payload field removed.
    // Discord guild is now singleton (settings.discord_guild_id).
    if (!draft.delivery_channel_id) {
      throw new Error("Discord delivery requires a channel");
    }
    const channelName = draft.delivery_channel_name || channelNameFallback;
    if (!channelName) {
      throw new Error("The selected Discord channel could not be resolved");
    }
    deliveryConfig = {
      delivery_mode: "discord_channel",
      channel_id: draft.delivery_channel_id,
      channel_name: channelName,
      deliver_on: draft.deliver_on,
      delivery_format: draft.delivery_format,
    };
  }

  if (draftWorkflowKind === AI_NEWS_KIND) {
    const guidedConfig = readAiNewsConfig(draft.execution_policy);
    if (!guidedConfig.agent_id) {
      throw new Error("AI News Briefing needs a research-capable agent");
    }

    return {
      name: draft.name.trim(),
      description: draft.description.trim() || null,
      board_id: draft.board_id || null,
      project_id: draft.project_id || null,
      trigger_type: draft.trigger_type,
      trigger_config: triggerConfig,
      status: draft.status,
      current_definition: { steps: [] },
      max_runtime_minutes: Math.max(1, Math.round(Number(draft.max_runtime_minutes) || 60)),
      policy_profile: draft.policy_profile.trim() || "safe",
      execution_policy: writeAiNewsConfig(draft.execution_policy, guidedConfig),
      delivery_config: deliveryConfig,
      reflect_on: draft.reflect_on.trim() || "manual",
      change_reason: changeReason.trim() || null,
    };
  }

  return {
    name: draft.name.trim(),
    description: draft.description.trim() || null,
    board_id: draft.board_id || null,
    project_id: draft.project_id || null,
    trigger_type: draft.trigger_type,
    trigger_config: triggerConfig,
    status: draft.status,
    current_definition: { steps },
    max_runtime_minutes: Math.max(1, Math.round(Number(draft.max_runtime_minutes) || 60)),
    policy_profile: draft.policy_profile.trim() || "safe",
    execution_policy: draft.execution_policy ?? null,
    delivery_config: deliveryConfig,
    reflect_on: draft.reflect_on.trim() || "manual",
    change_reason: changeReason.trim() || null,
  };
}

function SectionTitle({
  eyebrow,
  title,
  description,
}: {
  eyebrow: string;
  title: string;
  description: string;
}) {
  return (
    <div className="mb-4">
      <div
        className="text-[11px] uppercase tracking-[0.22em]"
        style={{ color: C.accent }}
      >
        {eyebrow}
      </div>
      <h2 className="mt-2 text-xl font-semibold" style={{ color: "var(--color-text-primary)" }}>
        {title}
      </h2>
      <p className="mt-1 text-sm leading-6" style={{ color: "var(--color-text-secondary)" }}>
        {description}
      </p>
    </div>
  );
}

function FieldLabel({ children }: { children: React.ReactNode }) {
  return (
    <label className="mb-2 block text-xs font-medium uppercase tracking-[0.14em]" style={{ color: "var(--color-text-muted)" }}>
      {children}
    </label>
  );
}

function InlineSection({
  title,
  description,
  children,
}: {
  title: string;
  description: string;
  children: React.ReactNode;
}) {
  return (
    <section>
      <div className="mb-4">
        <h3 className="text-base font-semibold" style={{ color: "var(--color-text-primary)" }}>
          {title}
        </h3>
        <p className="mt-1 text-sm leading-6" style={{ color: "var(--color-text-secondary)" }}>
          {description}
        </p>
      </div>
      {children}
    </section>
  );
}

const INPUT_CLASS =
  "w-full rounded-xl border px-3 py-2.5 text-sm outline-none transition-colors";

function inputStyle(multiline = false) {
  return {
    color: "var(--color-text-primary)",
    backgroundColor: "rgba(255,255,255,0.03)",
    borderColor: "rgba(255,255,255,0.08)",
    minHeight: multiline ? 120 : undefined,
  };
}

export default function WorkflowDetailClient({ workflowId }: { workflowId: string }) {
  const router = useRouter();
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState<WorkflowDraft | null>(null);
  const [activeTab, setActiveTab] = useState<WorkflowEditorTab>("builder");
  const [editorMode, setEditorMode] = useState<WorkflowEditorMode>("simple");
  const [isDirty, setIsDirty] = useState(false);
  const [syncedRevision, setSyncedRevision] = useState<string | null>(null);
  const [selectedStepIndex, setSelectedStepIndex] = useState(0);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [rollbackVersion, setRollbackVersion] = useState("");
  const [changeReason, setChangeReason] = useState("");

  const workflowQuery = useQuery({
    queryKey: ["workflow", workflowId],
    queryFn: () => api.workflows.get(workflowId),
  });
  const versionsQuery = useQuery({
    queryKey: ["workflow-versions", workflowId],
    queryFn: () => api.workflows.versions(workflowId),
  });
  const runsQuery = useQuery({
    queryKey: ["workflow-runs", workflowId],
    queryFn: () => api.workflows.runs(workflowId, 25),
    refetchInterval: (query) => {
      const runs = (query.state.data as WorkflowRun[] | undefined) ?? [];
      return runs.some((run) => run.status === "running" || run.status === "paused")
        ? 5_000
        : false;
    },
  });
  const boardsQuery = useQuery({
    queryKey: ["boards"],
    queryFn: () => api.boards.list(),
  });
  const agentsQuery = useQuery({
    queryKey: ["agents"],
    queryFn: () => api.agents.list(undefined, true),
  });
  // Phase 31 / OCS-15: gatewaysQuery removed. Discord guild is now a
  // singleton (settings.discord_guild_id) and channels come from the
  // dedicated /api/v1/discord/* router (Plan 29-01).

  const boardId = draft?.board_id || "";
  const projectsQuery = useQuery({
    queryKey: ["workflow-projects", boardId],
    queryFn: () => api.projects.list(boardId),
    enabled: Boolean(boardId),
  });

  const channelsQuery = useQuery({
    queryKey: ["workflow-discord-channels"],
    queryFn: () => api.discord.channels(),
    enabled: draft?.delivery_mode === "discord_channel",
  });

  const runDetailQuery = useQuery({
    queryKey: ["workflow-run-detail", selectedRunId],
    queryFn: () => api.workflows.runDetail(selectedRunId!),
    enabled: Boolean(selectedRunId),
    refetchInterval: (query) => {
      const run = (query.state.data as { run: WorkflowRun } | undefined)?.run;
      return run && (run.status === "running" || run.status === "paused") ? 4_000 : false;
    },
  });

  const workflow = workflowQuery.data;
  const runs = runsQuery.data ?? [];
  const boards = boardsQuery.data ?? [];
  const agents = agentsQuery.data ?? [];
  const aiNewsAgents = useMemo(() => selectAiNewsAgents(agents), [agents]);
  // Phase 31 / OCS-15: gateways / discordGateways removed. Single Discord
  // guild is configured in settings.discord_guild_id; channel list comes
  // from /api/v1/discord/channels (channelsQuery above).
  const projects = projectsQuery.data ?? [];
  const channels = channelsQuery.data ?? [];
  const channelQueryError = channelsQuery.isError ? formatError(channelsQuery.error) : null;
  const selectedRun = runs.find((run) => run.id === selectedRunId) ?? null;
  const activeRun =
    runs.find((run) => run.status === "running" || run.status === "paused") ?? null;
  const versions = useMemo(
    () => [...(versionsQuery.data ?? [])].sort((left, right) => right.version - left.version),
    [versionsQuery.data]
  );
  const rollbackOptions = versions.filter((version) => version.version !== workflow?.current_version);
  const selectedVersionOption =
    rollbackOptions.find((version) => String(version.version) === rollbackVersion) ?? null;
  const selectedStep =
    draft?.current_definition.steps[selectedStepIndex] ?? null;
  const currentWorkflowKind = workflowKind(draft?.execution_policy);
  const aiNewsConfig =
    currentWorkflowKind === AI_NEWS_KIND
      ? readAiNewsConfig(
          draft?.execution_policy,
          aiNewsAgents[0]?.id ??
            agents.find((agent) => agent.provision_status === "provisioned")?.id ??
            ""
        )
      : null;
  const primaryLlmStepIndex = useMemo(
    () => findPrimaryLlmStepIndex(draft?.current_definition.steps ?? []),
    [draft?.current_definition.steps]
  );
  const simpleGoal =
    primaryLlmStepIndex >= 0 && draft
      ? extractSimpleGoal(draft.current_definition.steps[primaryLlmStepIndex]?.input_template)
      : "";

  useSSE(sseUrls.workflows(), {
    enabled: Boolean(workflowId),
    onEvent: (_event, data) => {
      if (!data) return;
      const eventWorkflowId =
        typeof data.workflow_id === "string" ? data.workflow_id : null;
      const eventRunId = typeof data.run_id === "string" ? data.run_id : null;
      if (eventWorkflowId && eventWorkflowId !== workflowId) return;

      queryClient.invalidateQueries({ queryKey: ["workflow", workflowId] });
      queryClient.invalidateQueries({ queryKey: ["workflow-runs", workflowId] });
      queryClient.invalidateQueries({ queryKey: ["workflows"] });

      if (eventRunId) {
        queryClient.invalidateQueries({ queryKey: ["workflow-run-detail", eventRunId] });
      }
    },
  });

  useEffect(() => {
    if (!workflow) return;
    const revision = `${workflow.updated_at}:${workflow.current_version}`;
    if (!draft || draft.id !== workflow.id || (!isDirty && syncedRevision !== revision)) {
      setDraft(toWorkflowDraft(workflow));
      setSyncedRevision(revision);
      setIsDirty(false);
    }
  }, [workflow, draft, isDirty, syncedRevision]);

  useEffect(() => {
    if (!runs.length) {
      setSelectedRunId(null);
      return;
    }

    setSelectedRunId((current) => {
      if (current && runs.some((run) => run.id === current)) return current;
      return runs[0].id;
    });
  }, [runs]);

  useEffect(() => {
    const count = draft?.current_definition.steps.length ?? 0;
    setSelectedStepIndex((current) => {
      if (count === 0) return 0;
      return Math.min(current, count - 1);
    });
  }, [draft?.current_definition.steps.length]);

  useEffect(() => {
    setRollbackVersion("");
  }, [workflow?.current_version]);

  useEffect(() => {
    if (!draft || draft.delivery_mode !== "discord_channel" || !draft.delivery_channel_id) return;
    if (!channels.length) return;
    const selectedChannel = channels.find((channel) => channel.id === draft.delivery_channel_id);
    if (!selectedChannel) return;
    if (draft.delivery_channel_name === selectedChannel.name) return;
    setDraft({
      ...draft,
      delivery_channel_name: selectedChannel.name,
    });
  }, [channels, draft]);

  // Phase 31 / OCS-15: fallbackGatewayId effect removed. Single Discord guild
  // is configured server-side; no per-workflow gateway selection needed.

  function channelNameFallbackForDraft(current: WorkflowDraft): string | null {
    return channels.find((channel) => channel.id === current.delivery_channel_id)?.name ?? null;
  }

  const saveMutation = useMutation({
    mutationFn: () => {
      if (!draft) throw new Error("Workflow is still loading");
      const payload = buildWorkflowPayload(draft, {
        channelNameFallback: channelNameFallbackForDraft(draft),
        changeReason: "",
      });
      return api.workflows.update(workflowId, payload);
    },
    onSuccess: async (updated) => {
      const revision = `${updated.updated_at}:${updated.current_version}`;
      setDraft(toWorkflowDraft(updated));
      setSyncedRevision(revision);
      setIsDirty(false);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["workflow", workflowId] }),
        queryClient.invalidateQueries({ queryKey: ["workflows"] }),
      ]);
      notify.success("Workflow saved");
    },
    onError: (error) => notify.error(formatError(error)),
  });

  const createVersionMutation = useMutation({
    mutationFn: async () => {
      if (!draft) throw new Error("Workflow is still loading");

      let updatedWorkflow: WorkflowTemplate | null = null;
      if (isDirty) {
        const payload = buildWorkflowPayload(draft, {
          channelNameFallback: channelNameFallbackForDraft(draft),
          changeReason: "",
        });
        updatedWorkflow = await api.workflows.update(workflowId, payload);
      }

      const version = await api.workflows.createVersion(
        workflowId,
        changeReason.trim() || null
      );
      return { updatedWorkflow, version };
    },
    onSuccess: async ({ updatedWorkflow, version }) => {
      if (updatedWorkflow) {
        const revision = `${updatedWorkflow.updated_at}:${updatedWorkflow.current_version}`;
        setDraft(toWorkflowDraft(updatedWorkflow));
        setSyncedRevision(revision);
        setIsDirty(false);
      }
      setChangeReason("");
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["workflow", workflowId] }),
        queryClient.invalidateQueries({ queryKey: ["workflow-versions", workflowId] }),
        queryClient.invalidateQueries({ queryKey: ["workflows"] }),
      ]);
      notify.success(`Version v${version.version} created`);
    },
    onError: (error) => notify.error(formatError(error)),
  });

  const archiveMutation = useMutation({
    mutationFn: () => api.workflows.archive(workflowId),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["workflows"] });
      notify.success("Workflow archived");
      router.push("/workflows");
    },
    onError: (error) => notify.error(formatError(error)),
  });

  const runMutation = useMutation({
    mutationFn: () => api.workflows.run(workflowId),
    onSuccess: async (run) => {
      setSelectedRunId(run.id);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["workflow-runs", workflowId] }),
        queryClient.invalidateQueries({ queryKey: ["workflow-run-detail", run.id] }),
        queryClient.invalidateQueries({ queryKey: ["workflows"] }),
      ]);
      notify.success("Workflow run started");
    },
    onError: (error) => notify.error(formatError(error)),
  });

  const pauseMutation = useMutation({
    mutationFn: (runId: string) => api.workflows.pauseRun(runId),
    onSuccess: async (_, runId) => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["workflow-runs", workflowId] }),
        queryClient.invalidateQueries({ queryKey: ["workflow-run-detail", runId] }),
      ]);
      notify.info("Run paused");
    },
    onError: (error) => notify.error(formatError(error)),
  });

  const resumeMutation = useMutation({
    mutationFn: (runId: string) => api.workflows.resumeRun(runId),
    onSuccess: async (run) => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["workflow-runs", workflowId] }),
        queryClient.invalidateQueries({ queryKey: ["workflow-run-detail", run.id] }),
      ]);
      notify.success("Run resumed");
    },
    onError: (error) => notify.error(formatError(error)),
  });

  const stopMutation = useMutation({
    mutationFn: (runId: string) => api.workflows.stopRun(runId),
    onSuccess: async (_, runId) => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["workflow-runs", workflowId] }),
        queryClient.invalidateQueries({ queryKey: ["workflow-run-detail", runId] }),
      ]);
      notify.warning("Stop signal sent");
    },
    onError: (error) => notify.error(formatError(error)),
  });

  const rollbackMutation = useMutation({
    mutationFn: (version: number) => api.workflows.rollback(workflowId, version),
    onSuccess: async (updated) => {
      const revision = `${updated.updated_at}:${updated.current_version}`;
      setDraft(toWorkflowDraft(updated));
      setSyncedRevision(revision);
      setIsDirty(false);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["workflow", workflowId] }),
        queryClient.invalidateQueries({ queryKey: ["workflow-versions", workflowId] }),
        queryClient.invalidateQueries({ queryKey: ["workflows"] }),
      ]);
      notify.success("Workflow rolled back to an earlier version");
    },
    onError: (error) => notify.error(formatError(error)),
  });

  const deleteVersionMutation = useMutation({
    mutationFn: (version: number) => api.workflows.deleteVersion(workflowId, version),
    onSuccess: async () => {
      setRollbackVersion("");
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["workflow-versions", workflowId] }),
        queryClient.invalidateQueries({ queryKey: ["workflow", workflowId] }),
      ]);
      notify.success("Version deleted");
    },
    onError: (error) => notify.error(formatError(error)),
  });

  const deliverySummary = useMemo(() => {
    if (!draft) return "No delivery";
    if (draft.delivery_mode !== "discord_channel") return "No delivery";
    if (draft.delivery_channel_name) return `Discord • #${draft.delivery_channel_name}`;
    return "Discord • Choose channel";
  }, [draft]);

  // Phase 31 / OCS-15: gatewaySelectPlaceholder removed alongside the Gateway
  // select UI. Channel placeholder no longer gates on gateway selection.
  const channelSelectPlaceholder =
    draft?.delivery_mode !== "discord_channel"
      ? "Enable Discord delivery first"
      : channelsQuery.isLoading
        ? "Loading channels..."
        : channelQueryError
          ? "Could not load channels"
          : channels.length
            ? "Choose channel"
            : "No Discord channels found";

  function updateDraft(updater: (current: WorkflowDraft) => WorkflowDraft) {
    setDraft((current) => {
      if (!current) return current;
      return updater(current);
    });
    setIsDirty(true);
  }

  function updateStep(index: number, updater: (step: WorkflowDraftStep) => WorkflowDraftStep) {
    updateDraft((current) => ({
      ...current,
      current_definition: {
        steps: current.current_definition.steps.map((step, stepIndex) =>
          stepIndex === index ? updater(step) : step
        ),
      },
    }));
  }

  function updateSimpleGoal(goal: string) {
    if (primaryLlmStepIndex < 0) return;
    updateStep(primaryLlmStepIndex, (current) => ({
      ...current,
      input_template: applySimpleGoal(current.input_template, goal),
    }));
  }

  function updateAiNewsConfig(updater: (current: AiNewsGuidedConfig) => AiNewsGuidedConfig) {
    updateDraft((current) => {
      const nextCurrent = readAiNewsConfig(
        current.execution_policy,
        aiNewsAgents[0]?.id ??
          agents.find((agent) => agent.provision_status === "provisioned")?.id ??
          ""
      );
      return {
        ...current,
        execution_policy: writeAiNewsConfig(current.execution_policy, updater(nextCurrent)),
      };
    });
  }

  function addStep(preset: StepPresetKind = "llm") {
    const nextIndex = (draft?.current_definition.steps.length ?? 0) + 1;
    const step = createStepTemplate(nextIndex, agents, preset);
    updateDraft((current) => ({
      ...current,
      current_definition: {
        steps: [...current.current_definition.steps, step],
      },
    }));
    setSelectedStepIndex(nextIndex - 1);
  }

  function removeStep(index: number) {
    const currentCount = draft?.current_definition.steps.length ?? 0;
    updateDraft((current) => ({
      ...current,
      current_definition: {
        steps: current.current_definition.steps.filter((_, stepIndex) => stepIndex !== index),
      },
    }));
    if (currentCount <= 1) {
      setSelectedStepIndex(0);
      return;
    }
    setSelectedStepIndex((current) => {
      if (current > index) return current - 1;
      if (current === index) return Math.max(0, index - 1);
      return current;
    });
  }

  function duplicateStep(index: number) {
    updateDraft((current) => {
      const source = current.current_definition.steps[index];
      if (!source) return current;
      const nextIndex = index + 2;
      const copy: WorkflowDraftStep = {
        ...source,
        key: `${source.key || `step_${nextIndex}`}_copy`,
        name: source.name ? `${source.name} Copy` : `Step ${nextIndex}`,
      };
      const steps = [...current.current_definition.steps];
      steps.splice(index + 1, 0, copy);
      return {
        ...current,
        current_definition: { steps },
      };
    });
    setSelectedStepIndex(index + 1);
  }

  function moveStep(index: number, direction: -1 | 1) {
    updateDraft((current) => {
      const targetIndex = index + direction;
      if (
        targetIndex < 0 ||
        targetIndex >= current.current_definition.steps.length
      ) {
        return current;
      }
      const steps = [...current.current_definition.steps];
      const [step] = steps.splice(index, 1);
      steps.splice(targetIndex, 0, step);
      return {
        ...current,
        current_definition: { steps },
      };
    });

    setSelectedStepIndex((current) => {
      if (current === index) return index + direction;
      if (direction === -1 && current === index - 1) return index;
      if (direction === 1 && current === index + 1) return index;
      return current;
    });
  }

  if (workflowQuery.isError) {
    return (
      <AppShell>
        <GlassCard className="p-8">
          <h1 className="text-lg font-semibold" style={{ color: "var(--color-text-primary)" }}>
            Could not load workflow
          </h1>
          <p className="mt-2 text-sm" style={{ color: "var(--color-text-secondary)" }}>
            {formatError(workflowQuery.error)}
          </p>
          <Link
            href="/workflows"
            className="mt-5 inline-flex items-center gap-2 text-sm"
            style={{ color: C.accent }}
          >
            <ArrowLeft size={15} />
            Back to workflow list
          </Link>
        </GlassCard>
      </AppShell>
    );
  }

  if (workflowQuery.isLoading || !draft || !workflow) {
    return (
      <AppShell>
        <GlassCard className="p-8">
          <div className="flex items-center gap-3 text-sm" style={{ color: "var(--color-text-secondary)" }}>
            <RefreshCw size={16} className="animate-spin" />
            Loading workflow...
          </div>
        </GlassCard>
      </AppShell>
    );
  }

  const workflowStatus = WORKFLOW_STATUS_META[draft.status];

  return (
    <AppShell>
      <div className="space-y-6">
        <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
          <div>
            <Link
              href="/workflows"
              className="inline-flex items-center gap-2 text-sm"
              style={{ color: "var(--color-text-muted)" }}
            >
              <ArrowLeft size={15} />
              Back to workflows
            </Link>

            <div className="mt-4 flex flex-wrap items-center gap-3">
              <h1 className="text-3xl font-semibold tracking-tight" style={{ color: "var(--color-text-primary)" }}>
                {draft.name || "Workflow"}
              </h1>
              <span
                className="inline-flex items-center rounded-full px-2.5 py-1 text-[11px] font-medium"
                style={{
                  color: workflowStatus.text,
                  border: `1px solid ${workflowStatus.border}`,
                  backgroundColor: workflowStatus.bg,
                }}
              >
                {workflowStatus.label}
              </span>
              {isDirty && (
                <span
                  className="inline-flex items-center rounded-full px-2.5 py-1 text-[11px] font-medium"
                  style={{
                    color: C.warning,
                    border: "1px solid rgba(184,135,10,0.28)",
                    backgroundColor: "rgba(184,135,10,0.1)",
                  }}
                >
                  Unsaved
                </span>
              )}
            </div>

            <p className="mt-2 max-w-3xl text-sm leading-6" style={{ color: "var(--color-text-secondary)" }}>
              Triggers, versions, runs, and Discord delivery all stay in one place here.
            </p>
          </div>

          <div className="flex flex-wrap gap-3">
            <div
              className="flex items-center gap-2 rounded-xl px-3 py-2"
              style={{
                color: "var(--color-text-primary)",
                backgroundColor: "rgba(255,255,255,0.03)",
                border: "1px solid rgba(255,255,255,0.08)",
              }}
            >
              <span className="text-xs font-medium" style={{ color: "var(--color-text-muted)" }}>
                v{workflow.current_version}
              </span>
              <select
                value={rollbackVersion}
                onChange={(event) => setRollbackVersion(event.target.value)}
                className="rounded-lg px-2 py-1 text-xs"
                style={{
                  color: "var(--color-text-primary)",
                  backgroundColor: "rgba(255,255,255,0.03)",
                  border: "1px solid rgba(255,255,255,0.08)",
                }}
                disabled={versionsQuery.isLoading || rollbackOptions.length === 0}
              >
                <option value="">Versions</option>
                {rollbackOptions.map((version) => (
                  <option key={version.id} value={String(version.version)}>
                    {`v${version.version} • ${new Date(version.created_at).toLocaleDateString("de-CH")}`}
                  </option>
                ))}
              </select>
              <button
                type="button"
                onClick={() => {
                  const version = Number(rollbackVersion);
                  if (!version) return;
                  if (!window.confirm(`Roll back to version ${version}?`)) return;
                  rollbackMutation.mutate(version);
                }}
                disabled={
                  rollbackMutation.isPending ||
                  deleteVersionMutation.isPending ||
                  !rollbackVersion
                }
                className="rounded-lg px-2.5 py-1 text-xs font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-60"
                style={{
                  color: "var(--color-text-primary)",
                  backgroundColor: "rgba(255,255,255,0.04)",
                  border: "1px solid rgba(255,255,255,0.08)",
                }}
              >
                Restore
              </button>
              <button
                type="button"
                onClick={() => {
                  const version = Number(rollbackVersion);
                  if (!version) return;
                  if (!window.confirm(`Delete version ${version}? This cannot be undone.`)) return;
                  deleteVersionMutation.mutate(version);
                }}
                disabled={
                  rollbackMutation.isPending ||
                  deleteVersionMutation.isPending ||
                  !rollbackVersion
                }
                className="rounded-lg px-2.5 py-1 text-xs font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-60"
                style={{
                  color: "var(--color-status-error)",
                  backgroundColor: "rgba(239,68,68,0.08)",
                  border: "1px solid rgba(239,68,68,0.18)",
                }}
                title={
                  selectedVersionOption
                    ? `Delete version ${selectedVersionOption.version}`
                    : undefined
                }
              >
                Delete
              </button>
            </div>

            <button
              type="button"
              onClick={() => saveMutation.mutate()}
              disabled={saveMutation.isPending || createVersionMutation.isPending}
              className="inline-flex items-center gap-2 rounded-xl px-4 py-2 text-sm font-medium text-white transition-opacity disabled:cursor-not-allowed disabled:opacity-60"
              style={{
                background: `linear-gradient(135deg, ${C.accent}, ${C.accentHover})`,
              }}
            >
              {saveMutation.isPending ? (
                <RefreshCw size={15} className="animate-spin" />
              ) : (
                <Save size={15} />
              )}
              Save
            </button>

            <button
              type="button"
              onClick={() => createVersionMutation.mutate()}
              disabled={saveMutation.isPending || createVersionMutation.isPending}
              className="inline-flex items-center gap-2 rounded-xl px-4 py-2 text-sm transition-colors disabled:cursor-not-allowed disabled:opacity-60"
              style={{
                color: "var(--color-text-primary)",
                backgroundColor: "rgba(255,255,255,0.03)",
                border: "1px solid rgba(255,255,255,0.08)",
              }}
              title="Save the current state as a reusable version snapshot"
            >
              {createVersionMutation.isPending ? (
                <RefreshCw size={15} className="animate-spin" />
              ) : (
                <History size={15} />
              )}
              Create version
            </button>

            <button
              type="button"
              onClick={() => runMutation.mutate()}
              disabled={
                runMutation.isPending ||
                saveMutation.isPending ||
                createVersionMutation.isPending ||
                Boolean(activeRun) ||
                workflow.status !== "active"
              }
              className="inline-flex items-center gap-2 rounded-xl px-4 py-2 text-sm transition-colors disabled:cursor-not-allowed disabled:opacity-60"
              style={{
                color: "var(--color-text-primary)",
                backgroundColor: "rgba(255,255,255,0.03)",
                border: "1px solid rgba(255,255,255,0.08)",
              }}
              title={
                workflow.status !== "active"
                  ? "Save the workflow as active before running it"
                  : activeRun
                    ? "A run is already in progress"
                    : undefined
              }
            >
              {runMutation.isPending ? (
                <RefreshCw size={15} className="animate-spin" />
              ) : (
                <Play size={15} />
              )}
              Run now
            </button>

            <button
              type="button"
              onClick={() => {
                if (!window.confirm("Archive this workflow?")) return;
                archiveMutation.mutate();
              }}
              disabled={archiveMutation.isPending}
              className="inline-flex items-center gap-2 rounded-xl px-4 py-2 text-sm transition-colors disabled:cursor-not-allowed disabled:opacity-60"
              style={{
                color: "var(--color-status-error)",
                backgroundColor: "rgba(239,68,68,0.08)",
                border: "1px solid rgba(239,68,68,0.18)",
              }}
            >
              {archiveMutation.isPending ? (
                <RefreshCw size={15} className="animate-spin" />
              ) : (
                <Trash2 size={15} />
              )}
              Archive
            </button>
          </div>
        </div>

        <div className="grid gap-3 lg:grid-cols-4">
          <GlassCard className="p-4">
            <div className="text-xs uppercase tracking-[0.18em]" style={{ color: "var(--color-text-muted)" }}>
              Trigger
            </div>
            <div className="mt-2 text-lg font-semibold" style={{ color: "var(--color-text-primary)" }}>
              {formatWorkflowTriggerLabel(draft.trigger_type, draft.trigger_config)}
            </div>
            <div className="mt-2 text-xs" style={{ color: "var(--color-text-muted)" }}>
              Next run: {formatDateTime(workflow.next_run_at)}
            </div>
          </GlassCard>

          <GlassCard className="p-4">
            <div className="text-xs uppercase tracking-[0.18em]" style={{ color: "var(--color-text-muted)" }}>
              Steps
            </div>
            <div className="mt-2 text-lg font-semibold" style={{ color: "var(--color-text-primary)" }}>
              {draft.current_definition.steps.length}
            </div>
            <div className="mt-2 text-xs" style={{ color: "var(--color-text-muted)" }}>
              Version {workflow.current_version} • {draft.status === "active" ? "ready to run" : "not live yet"}
            </div>
          </GlassCard>

          <GlassCard className="p-4">
            <div className="text-xs uppercase tracking-[0.18em]" style={{ color: "var(--color-text-muted)" }}>
              Delivery
            </div>
            <div className="mt-2 text-lg font-semibold" style={{ color: "var(--color-text-primary)" }}>
              {deliverySummary}
            </div>
            <div className="mt-2 text-xs" style={{ color: "var(--color-text-muted)" }}>
              Format {draft.delivery_format} • {draft.deliver_on}
            </div>
          </GlassCard>

          <GlassCard className="p-4">
            <div className="text-xs uppercase tracking-[0.18em]" style={{ color: "var(--color-text-muted)" }}>
              Last updated
            </div>
            <div className="mt-2 text-lg font-semibold" style={{ color: "var(--color-text-primary)" }}>
              {timeAgo(workflow.updated_at)}
            </div>
            <div className="mt-2 text-xs" style={{ color: "var(--color-text-muted)" }}>
              Validated: {formatDateTime(workflow.last_validated_at)}
            </div>
          </GlassCard>
        </div>

        <div className="flex gap-0 border-b" style={{ borderColor: "rgba(255,255,255,0.08)" }}>
          {[
            { id: "builder" as const, label: "Builder" },
            { id: "runs" as const, label: `Runs${runs.length ? ` (${runs.length})` : ""}` },
          ].map((tab) => (
            <button
              key={tab.id}
              type="button"
              onClick={() => setActiveTab(tab.id)}
              className="px-4 py-2.5 text-sm font-medium transition-all"
              style={{
                color:
                  activeTab === tab.id
                    ? "var(--color-text-primary)"
                    : "var(--color-text-muted)",
                background: "transparent",
                border: "none",
                borderBottomStyle: "solid",
                borderBottomWidth: 2,
                borderBottomColor:
                  activeTab === tab.id ? C.accent : "transparent",
                marginBottom: -1,
              }}
            >
              {tab.label}
            </button>
          ))}
        </div>

        {activeTab === "builder" ? (
          <div className="space-y-6">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <div className="text-sm font-medium" style={{ color: "var(--color-text-primary)" }}>
                  Editor mode
                </div>
                <div className="mt-1 text-sm" style={{ color: "var(--color-text-secondary)" }}>
                  Guided mode keeps the setup simple. Technical mode is there only when you want to inspect or edit raw steps.
                </div>
              </div>
              <div
                className="inline-flex rounded-2xl p-1"
                style={{
                  backgroundColor: "rgba(255,255,255,0.03)",
                  border: "1px solid rgba(255,255,255,0.08)",
                }}
              >
                {[
                  { id: "simple" as const, label: "Guided" },
                  { id: "advanced" as const, label: "Technical" },
                ].map((mode) => {
                  const active = editorMode === mode.id;
                  return (
                    <button
                      key={mode.id}
                      type="button"
                      onClick={() => setEditorMode(mode.id)}
                      className="rounded-xl px-4 py-2 text-sm font-medium transition-colors"
                      style={{
                        color: active ? "white" : "var(--color-text-secondary)",
                        background: active ? `linear-gradient(135deg, ${C.accent}, ${C.accentHover})` : "transparent",
                      }}
                    >
                      {mode.label}
                    </button>
                  );
                })}
              </div>
            </div>

            {editorMode === "simple" ? (
              <div className="space-y-6">
                {currentWorkflowKind === AI_NEWS_KIND && aiNewsConfig ? (
                  <GlassCard className="p-5">
                    <SectionTitle
                      eyebrow="Guided"
                      title="Configure your AI News Briefing"
                      description="Tell Mission Control what kind of briefing you want. The backend keeps the internal research and writing steps in sync for you."
                    />

                    <div className="grid gap-4 md:grid-cols-2">
                      <div className="md:col-span-2">
                        <FieldLabel>Name</FieldLabel>
                        <input
                          value={draft.name}
                          onChange={(event) =>
                            updateDraft((current) => ({ ...current, name: event.target.value }))
                          }
                          className={INPUT_CLASS}
                          style={inputStyle()}
                          placeholder="AI News Briefing"
                        />
                      </div>

                      <div className="md:col-span-2">
                        <FieldLabel>Workflow summary</FieldLabel>
                        <textarea
                          value={draft.description}
                          onChange={(event) =>
                            updateDraft((current) => ({ ...current, description: event.target.value }))
                          }
                          className={cn(INPUT_CLASS, "resize-y")}
                          style={inputStyle(true)}
                          placeholder="A concise AI news digest with the most important stories, fact checks, impact notes, and an OpenClaw corner."
                        />
                      </div>

                      <div>
                        <FieldLabel>Briefing agent</FieldLabel>
                        <select
                          value={aiNewsConfig.agent_id}
                          onChange={(event) =>
                            updateAiNewsConfig((current) => ({
                              ...current,
                              agent_id: event.target.value,
                            }))
                          }
                          className={INPUT_CLASS}
                          style={inputStyle()}
                        >
                          <option value="">Choose an agent</option>
                          {aiNewsAgents.map((agent) => (
                            <option key={agent.id} value={agent.id}>
                              {agent.name}{agent.role ? ` • ${agent.role}` : ""}
                            </option>
                          ))}
                        </select>
                        {aiNewsAgents.length ? (
                          <p className="mt-2 text-xs leading-5" style={{ color: "var(--color-text-muted)" }}>
                            Research-oriented agents are sorted first. Any agent with a live gateway connection can be used here.
                          </p>
                        ) : (
                          <p className="mt-2 text-xs leading-5" style={{ color: "var(--color-text-muted)" }}>
                            Add or connect an agent with a live gateway connection first. MC uses it to research and write the final briefing.
                          </p>
                        )}
                      </div>

                      <div>
                        <FieldLabel>Stories to include</FieldLabel>
                        <input
                          type="number"
                          min={3}
                          max={10}
                          value={aiNewsConfig.max_items}
                          onChange={(event) =>
                            updateAiNewsConfig((current) => ({
                              ...current,
                              max_items: Math.min(10, Math.max(3, Number(event.target.value) || 5)),
                            }))
                          }
                          className={INPUT_CLASS}
                          style={inputStyle()}
                        />
                      </div>

                      <div>
                        <FieldLabel>Time window (hours)</FieldLabel>
                        <input
                          type="number"
                          min={6}
                          max={72}
                          value={aiNewsConfig.timeframe_hours}
                          onChange={(event) =>
                            updateAiNewsConfig((current) => ({
                              ...current,
                              timeframe_hours: Math.min(72, Math.max(6, Number(event.target.value) || 24)),
                            }))
                          }
                          className={INPUT_CLASS}
                          style={inputStyle()}
                        />
                      </div>

                      <div>
                        <FieldLabel>Source profile</FieldLabel>
                        <select
                          value={aiNewsConfig.source_profile}
                          onChange={(event) =>
                            updateAiNewsConfig((current) => ({
                              ...current,
                              source_profile: event.target.value as AiNewsGuidedConfig["source_profile"],
                            }))
                          }
                          className={INPUT_CLASS}
                          style={inputStyle()}
                        >
                          <option value="official">Official sources first</option>
                          <option value="balanced">Balanced mix</option>
                          <option value="broad">Broad discovery</option>
                        </select>
                      </div>

                      <div>
                        <FieldLabel>Fact-check level</FieldLabel>
                        <select
                          value={aiNewsConfig.fact_check_level}
                          onChange={(event) =>
                            updateAiNewsConfig((current) => ({
                              ...current,
                              fact_check_level: event.target.value as AiNewsGuidedConfig["fact_check_level"],
                            }))
                          }
                          className={INPUT_CLASS}
                          style={inputStyle()}
                        >
                          <option value="fast">Fast</option>
                          <option value="balanced">Balanced</option>
                          <option value="strict">Strict</option>
                        </select>
                      </div>

                      <div className="md:col-span-2">
                        <FieldLabel>Topic focus</FieldLabel>
                        <input
                          value={aiNewsConfig.topic_focus}
                          onChange={(event) =>
                            updateAiNewsConfig((current) => ({
                              ...current,
                              topic_focus: event.target.value,
                            }))
                          }
                          className={INPUT_CLASS}
                          style={inputStyle()}
                          placeholder="Optional: enterprise AI, model launches, policy, coding agents..."
                        />
                      </div>

                      <div className="md:col-span-2">
                        <FieldLabel>Editorial guidance</FieldLabel>
                        <textarea
                          value={aiNewsConfig.custom_instructions}
                          onChange={(event) =>
                            updateAiNewsConfig((current) => ({
                              ...current,
                              custom_instructions: event.target.value,
                            }))
                          }
                          className={cn(INPUT_CLASS, "resize-y")}
                          style={inputStyle(true)}
                          placeholder="Optional: Use emojis sparingly, keep it concise, highlight major business impact, mention uncertainty clearly..."
                        />
                      </div>

                      <div className="md:col-span-2 grid gap-3 md:grid-cols-3">
                        <label
                          className="rounded-2xl border px-4 py-3 text-sm"
                          style={{
                            borderColor: "rgba(255,255,255,0.08)",
                            backgroundColor: "rgba(255,255,255,0.02)",
                            color: "var(--color-text-primary)",
                          }}
                        >
                          <div className="flex items-center justify-between gap-3">
                            <span>Include impact notes</span>
                            <input
                              type="checkbox"
                              checked={aiNewsConfig.include_impacts}
                              onChange={(event) =>
                                updateAiNewsConfig((current) => ({
                                  ...current,
                                  include_impacts: event.target.checked,
                                }))
                              }
                            />
                          </div>
                        </label>

                        <label
                          className="rounded-2xl border px-4 py-3 text-sm"
                          style={{
                            borderColor: "rgba(255,255,255,0.08)",
                            backgroundColor: "rgba(255,255,255,0.02)",
                            color: "var(--color-text-primary)",
                          }}
                        >
                          <div className="flex items-center justify-between gap-3">
                            <span>Use emojis</span>
                            <input
                              type="checkbox"
                              checked={aiNewsConfig.include_emojis}
                              onChange={(event) =>
                                updateAiNewsConfig((current) => ({
                                  ...current,
                                  include_emojis: event.target.checked,
                                }))
                              }
                            />
                          </div>
                        </label>

                        <label
                          className="rounded-2xl border px-4 py-3 text-sm"
                          style={{
                            borderColor: "rgba(255,255,255,0.08)",
                            backgroundColor: "rgba(255,255,255,0.02)",
                            color: "var(--color-text-primary)",
                          }}
                        >
                          <div className="flex items-center justify-between gap-3">
                            <span>Include OpenClaw corner</span>
                            <input
                              type="checkbox"
                              checked={aiNewsConfig.include_openclaw_corner}
                              onChange={(event) =>
                                updateAiNewsConfig((current) => ({
                                  ...current,
                                  include_openclaw_corner: event.target.checked,
                                }))
                              }
                            />
                          </div>
                        </label>
                      </div>

                      {aiNewsConfig.include_openclaw_corner && (
                        <div>
                          <FieldLabel>OpenClaw items</FieldLabel>
                          <input
                            type="number"
                            min={1}
                            max={5}
                            value={aiNewsConfig.openclaw_items}
                            onChange={(event) =>
                              updateAiNewsConfig((current) => ({
                                ...current,
                                openclaw_items: Math.min(5, Math.max(1, Number(event.target.value) || 1)),
                              }))
                            }
                            className={INPUT_CLASS}
                            style={inputStyle()}
                          />
                        </div>
                      )}
                    </div>
                  </GlassCard>
                ) : (
                  <GlassCard className="p-5">
                    <SectionTitle
                      eyebrow="Guided"
                      title="What should this workflow do?"
                      description="Describe the outcome you want. Mission Control will keep the technical steps behind the scenes."
                    />

                    <div className="grid gap-4 md:grid-cols-2">
                      <div className="md:col-span-2">
                        <FieldLabel>Name</FieldLabel>
                        <input
                          value={draft.name}
                          onChange={(event) =>
                            updateDraft((current) => ({ ...current, name: event.target.value }))
                          }
                          className={INPUT_CLASS}
                          style={inputStyle()}
                          placeholder="AI News Briefing"
                        />
                      </div>

                      <div className="md:col-span-2">
                        <FieldLabel>Summary</FieldLabel>
                        <textarea
                          value={draft.description}
                          onChange={(event) =>
                            updateDraft((current) => ({ ...current, description: event.target.value }))
                          }
                          className={cn(INPUT_CLASS, "resize-y")}
                          style={inputStyle(true)}
                          placeholder="Describe the recurring result you want in plain language."
                        />
                      </div>

                      <div>
                        <FieldLabel>Board</FieldLabel>
                        <select
                          value={draft.board_id}
                          onChange={(event) =>
                            updateDraft((current) => ({
                              ...current,
                              board_id: event.target.value,
                              project_id: "",
                            }))
                          }
                          className={INPUT_CLASS}
                          style={inputStyle()}
                        >
                          <option value="">No board</option>
                          {boards.map((board) => (
                            <option key={board.id} value={board.id}>
                              {board.name}
                            </option>
                          ))}
                        </select>
                      </div>

                      <div>
                        <FieldLabel>Project</FieldLabel>
                        <select
                          value={draft.project_id}
                          onChange={(event) =>
                            updateDraft((current) => ({
                              ...current,
                              project_id: event.target.value,
                            }))
                          }
                          className={INPUT_CLASS}
                          style={inputStyle()}
                          disabled={!draft.board_id}
                        >
                          <option value="">No project</option>
                          {projects.map((project) => (
                            <option key={project.id} value={project.id}>
                              {project.name}
                            </option>
                          ))}
                        </select>
                      </div>

                      {primaryLlmStepIndex >= 0 && (
                        <div className="md:col-span-2">
                          <FieldLabel>Extra guidance for the output</FieldLabel>
                          <textarea
                            value={simpleGoal}
                            onChange={(event) => updateSimpleGoal(event.target.value)}
                            className={cn(INPUT_CLASS, "resize-y")}
                            style={inputStyle(true)}
                            placeholder="For example: concise, use emojis, include fact checks, focus on impact."
                          />
                          <p className="mt-2 text-xs leading-5" style={{ color: "var(--color-text-muted)" }}>
                            These notes are added to the main LLM step automatically, so you do not have to edit a prompt template.
                          </p>
                        </div>
                      )}
                    </div>
                  </GlassCard>
                )}

                <GlassCard className="p-5">
                  <SectionTitle
                    eyebrow="Timing"
                    title="When should it run?"
                    description="Choose the timing here. Mission Control will handle the scheduler details in the background."
                  />

                  <div className="grid gap-4 md:grid-cols-2">
                    <div>
                      <FieldLabel>Start</FieldLabel>
                      <select
                        value={draft.trigger_type === "scheduled" ? "scheduled" : "manual"}
                        onChange={(event) =>
                          updateDraft((current) => ({
                            ...current,
                            trigger_type:
                              event.target.value === "scheduled" ? "scheduled" : "manual",
                            trigger_config:
                              event.target.value === "scheduled"
                                ? {
                                    schedule_type: "daily",
                                    schedule_time: "08:00",
                                  }
                                : current.trigger_config,
                          }))
                        }
                        className={INPUT_CLASS}
                        style={inputStyle()}
                      >
                        <option value="manual">I will run it manually</option>
                        <option value="scheduled">Run it on a schedule</option>
                      </select>
                    </div>

                    {draft.trigger_type === "scheduled" && (
                      <>
                        <div>
                          <FieldLabel>Cadence</FieldLabel>
                          <select
                            value={String(draft.trigger_config.schedule_type ?? "daily")}
                            onChange={(event) =>
                              updateDraft((current) => ({
                                ...current,
                                trigger_config:
                                  event.target.value === "interval"
                                    ? {
                                        schedule_type: "interval",
                                        schedule_interval_hours: 24,
                                      }
                                    : event.target.value === "weekly"
                                      ? {
                                          schedule_type: "weekly",
                                          schedule_day: "mon",
                                          schedule_time: "08:30",
                                        }
                                      : {
                                          schedule_type: event.target.value,
                                          schedule_time: "08:00",
                                        },
                              }))
                            }
                            className={INPUT_CLASS}
                            style={inputStyle()}
                          >
                            <option value="daily">Daily</option>
                            <option value="weekdays">Weekdays</option>
                            <option value="weekly">Weekly</option>
                            <option value="interval">Every X hours</option>
                          </select>
                        </div>

                        {draft.trigger_config.schedule_type === "interval" ? (
                          <div>
                            <FieldLabel>How many hours apart?</FieldLabel>
                            <input
                              type="number"
                              min={1}
                              value={Number(draft.trigger_config.schedule_interval_hours ?? 24)}
                              onChange={(event) =>
                                updateDraft((current) => ({
                                  ...current,
                                  trigger_config: {
                                    ...current.trigger_config,
                                    schedule_interval_hours: Number(event.target.value) || 24,
                                  },
                                }))
                              }
                              className={INPUT_CLASS}
                              style={inputStyle()}
                            />
                          </div>
                        ) : draft.trigger_config.schedule_type === "weekly" ? (
                          <>
                            <div>
                              <FieldLabel>Day of week</FieldLabel>
                              <select
                                value={String(draft.trigger_config.schedule_day ?? "mon")}
                                onChange={(event) =>
                                  updateDraft((current) => ({
                                    ...current,
                                    trigger_config: {
                                      ...current.trigger_config,
                                      schedule_day: event.target.value,
                                    },
                                  }))
                                }
                                className={INPUT_CLASS}
                                style={inputStyle()}
                              >
                                {WEEKDAY_OPTIONS.map((option) => (
                                  <option key={option.value} value={option.value}>
                                    {option.label}
                                  </option>
                                ))}
                              </select>
                            </div>

                            <div>
                              <FieldLabel>Time</FieldLabel>
                              <input
                                type="time"
                                value={String(draft.trigger_config.schedule_time ?? "08:30")}
                                onChange={(event) =>
                                  updateDraft((current) => ({
                                    ...current,
                                    trigger_config: {
                                      ...current.trigger_config,
                                      schedule_time: event.target.value,
                                    },
                                  }))
                                }
                                className={INPUT_CLASS}
                                style={inputStyle()}
                              />
                            </div>
                          </>
                        ) : (
                          <div>
                            <FieldLabel>Time</FieldLabel>
                            <input
                              type="time"
                              value={String(draft.trigger_config.schedule_time ?? "08:00")}
                              onChange={(event) =>
                                updateDraft((current) => ({
                                  ...current,
                                  trigger_config: {
                                    ...current.trigger_config,
                                    schedule_time: event.target.value,
                                  },
                                }))
                              }
                              className={INPUT_CLASS}
                              style={inputStyle()}
                            />
                          </div>
                        )}
                      </>
                    )}
                  </div>
                </GlassCard>

                <GlassCard className="p-5">
                  <SectionTitle
                    eyebrow="Delivery"
                    title="Where should the result go?"
                    description="Choose the destination. The advanced delivery plumbing stays out of the way unless you need it."
                  />

                  <div className="grid gap-4 md:grid-cols-2">
                    <div>
                      <FieldLabel>Send results to</FieldLabel>
                      <select
                        value={draft.delivery_mode}
                        onChange={(event) =>
                          updateDraft((current) => ({
                            ...current,
                            delivery_mode: event.target.value as WorkflowDraft["delivery_mode"],
                            delivery_channel_id:
                              event.target.value === "discord_channel"
                                ? current.delivery_channel_id
                                : "",
                            delivery_channel_name:
                              event.target.value === "discord_channel"
                                ? current.delivery_channel_name
                                : "",
                          }))
                        }
                        className={INPUT_CLASS}
                        style={inputStyle()}
                      >
                        <option value="none">Do not send anywhere yet</option>
                        <option value="discord_channel">Send to Discord</option>
                      </select>
                    </div>

                    <div>
                      <FieldLabel>Status</FieldLabel>
                      <select
                        value={draft.status === "active" ? "active" : "draft"}
                        onChange={(event) =>
                          updateDraft((current) => ({
                            ...current,
                            status:
                              event.target.value === "active"
                                ? "active"
                                : current.status === "archived"
                                  ? "archived"
                                  : "draft",
                          }))
                        }
                        className={INPUT_CLASS}
                        style={inputStyle()}
                      >
                        <option value="draft">Draft</option>
                        <option value="active">Active</option>
                      </select>
                    </div>

                    {draft.delivery_mode === "discord_channel" && (
                      <div>
                        <FieldLabel>Discord channel</FieldLabel>
                        <select
                          value={draft.delivery_channel_id}
                          onChange={(event) => {
                            const channel = channels.find((item) => item.id === event.target.value);
                            updateDraft((current) => ({
                              ...current,
                              delivery_channel_id: event.target.value,
                              delivery_channel_name: channel?.name ?? "",
                            }));
                          }}
                          className={INPUT_CLASS}
                          style={inputStyle()}
                          disabled={
                            channelsQuery.isLoading ||
                            Boolean(channelQueryError)
                          }
                        >
                          <option value="">{channelSelectPlaceholder}</option>
                          {channels.map((channel) => (
                            <option key={channel.id} value={channel.id}>
                              #{channel.name}
                            </option>
                          ))}
                        </select>
                        {channelQueryError && (
                          <p className="mt-2 text-xs leading-5" style={{ color: "var(--color-status-error)" }}>
                            {channelQueryError}
                          </p>
                        )}
                      </div>
                    )}
                  </div>
                </GlassCard>

                <GlassCard className="p-5">
                  <SectionTitle
                    eyebrow="Behind the scenes"
                    title="What Mission Control handles for you"
                    description="This is the internal workflow plan generated from your guided setup. You only need technical mode if you want to fine-tune the underlying steps."
                  />

                  {draft.current_definition.steps.length ? (
                    <div className="grid gap-3 md:grid-cols-2">
                      {draft.current_definition.steps.map((step, index) => (
                        <div
                          key={`${step.key}-${index}`}
                          className="rounded-2xl border p-4"
                          style={{
                            borderColor: "rgba(255,255,255,0.08)",
                            backgroundColor: "rgba(255,255,255,0.02)",
                          }}
                        >
                          <div className="text-xs uppercase tracking-[0.18em]" style={{ color: C.accent }}>
                            Step {index + 1}
                          </div>
                          <div className="mt-2 text-sm font-medium" style={{ color: "var(--color-text-primary)" }}>
                            {step.name || `Step ${index + 1}`}
                          </div>
                          <div className="mt-1 text-sm leading-6" style={{ color: "var(--color-text-secondary)" }}>
                            {describeSimpleFlow(step, agents)}
                          </div>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <div
                      className="rounded-2xl border border-dashed px-5 py-6 text-sm"
                      style={{
                        color: "var(--color-text-secondary)",
                        borderColor: "rgba(255,255,255,0.12)",
                      }}
                    >
                      This workflow does not have internal steps yet. If you want full manual control, switch to Technical mode below.
                    </div>
                  )}

                  <div className="mt-4 flex flex-wrap items-center justify-between gap-3 rounded-2xl px-4 py-4" style={{
                    border: "1px solid rgba(255,255,255,0.08)",
                    backgroundColor: "rgba(255,255,255,0.03)",
                  }}>
                    <div className="text-sm" style={{ color: "var(--color-text-secondary)" }}>
                      Most workflows never need step-by-step tuning. If you do, you can still open the technical editor.
                    </div>
                    <button
                      type="button"
                      onClick={() => setEditorMode("advanced")}
                      className="rounded-xl px-4 py-2 text-sm font-medium"
                      style={{
                        color: "var(--color-text-primary)",
                        border: "1px solid rgba(255,255,255,0.08)",
                        backgroundColor: "rgba(255,255,255,0.03)",
                      }}
                    >
                      Open technical editor
                    </button>
                  </div>
                </GlassCard>
              </div>
            ) : (
              <>
            <GlassCard className="p-5">
              <SectionTitle
                eyebrow="Builder"
                title="Workflow-Setup"
                description="The most important settings are bundled here in one compact area. No more jumping between several separate cards."
              />

              <div className="grid gap-6 xl:grid-cols-[minmax(0,1.2fr)_minmax(320px,0.8fr)]">
                <div className="space-y-6">
                  <InlineSection
                    title="Basics"
                    description="Name, status, context, and runtime limits. That's everything you really need for a solid start."
                  >
                    <div className="grid gap-4 md:grid-cols-2">
                      <div className="md:col-span-2">
                        <FieldLabel>Name</FieldLabel>
                        <input
                          value={draft.name}
                          onChange={(event) =>
                            updateDraft((current) => ({ ...current, name: event.target.value }))
                          }
                          className={INPUT_CLASS}
                          style={inputStyle()}
                          placeholder="Weekly Planning Digest"
                        />
                      </div>

                      <div className="md:col-span-2">
                        <FieldLabel>Beschreibung</FieldLabel>
                        <textarea
                          value={draft.description}
                          onChange={(event) =>
                            updateDraft((current) => ({ ...current, description: event.target.value }))
                          }
                          className={cn(INPUT_CLASS, "resize-y")}
                          style={inputStyle(true)}
                          placeholder="What should this workflow reliably automate?"
                        />
                      </div>

                      <div>
                        <FieldLabel>Status</FieldLabel>
                        <select
                          value={draft.status}
                          onChange={(event) =>
                            updateDraft((current) => ({
                              ...current,
                              status: event.target.value as WorkflowTemplate["status"],
                            }))
                          }
                          className={INPUT_CLASS}
                          style={inputStyle()}
                        >
                          <option value="draft">Draft</option>
                          <option value="validated">Validiert</option>
                          <option value="active">Active</option>
                          <option value="archived">Archiv</option>
                        </select>
                      </div>

                      <div>
                        <FieldLabel>Max. Laufzeit (Minuten)</FieldLabel>
                        <input
                          type="number"
                          min={1}
                          max={1440}
                          value={draft.max_runtime_minutes}
                          onChange={(event) =>
                            updateDraft((current) => ({
                              ...current,
                              max_runtime_minutes: Number(event.target.value) || 60,
                            }))
                          }
                          className={INPUT_CLASS}
                          style={inputStyle()}
                        />
                      </div>

                      <div>
                        <FieldLabel>Board</FieldLabel>
                        <select
                          value={draft.board_id}
                          onChange={(event) =>
                            updateDraft((current) => ({
                              ...current,
                              board_id: event.target.value,
                              project_id: "",
                            }))
                          }
                          className={INPUT_CLASS}
                          style={inputStyle()}
                        >
                          <option value="">No board</option>
                          {boards.map((board) => (
                            <option key={board.id} value={board.id}>
                              {board.name}
                            </option>
                          ))}
                        </select>
                      </div>

                      <div>
                        <FieldLabel>Projekt</FieldLabel>
                        <select
                          value={draft.project_id}
                          onChange={(event) =>
                            updateDraft((current) => ({
                              ...current,
                              project_id: event.target.value,
                            }))
                          }
                          className={INPUT_CLASS}
                          style={inputStyle()}
                          disabled={!draft.board_id}
                        >
                          <option value="">No project</option>
                          {projects.map((project) => (
                            <option key={project.id} value={project.id}>
                              {project.name}
                            </option>
                          ))}
                        </select>
                      </div>

                      <div>
                        <FieldLabel>Policy Profile</FieldLabel>
                        <select
                          value={draft.policy_profile}
                          onChange={(event) =>
                            updateDraft((current) => ({
                              ...current,
                              policy_profile: event.target.value,
                            }))
                          }
                          className={INPUT_CLASS}
                          style={inputStyle()}
                        >
                          <option value="safe">safe</option>
                          <option value="balanced">balanced</option>
                          <option value="open">open</option>
                        </select>
                      </div>

                      <div>
                        <FieldLabel>Reflection</FieldLabel>
                        <select
                          value={draft.reflect_on}
                          onChange={(event) =>
                            updateDraft((current) => ({
                              ...current,
                              reflect_on: event.target.value,
                            }))
                          }
                          className={INPUT_CLASS}
                          style={inputStyle()}
                        >
                          <option value="manual">manual</option>
                          <option value="on_failure">on_failure</option>
                          <option value="always">always</option>
                        </select>
                      </div>
                    </div>
                  </InlineSection>

                  <div className="h-px" style={{ backgroundColor: "rgba(255,255,255,0.08)" }} />

                  <InlineSection
                    title="Trigger"
                    description="Decide here whether the workflow starts manually or runs on a schedule."
                  >
                    <div className="grid gap-4 md:grid-cols-2">
                      <div>
                        <FieldLabel>Trigger-Typ</FieldLabel>
                        <select
                          value={draft.trigger_type}
                          onChange={(event) =>
                            updateDraft((current) => ({
                              ...current,
                              trigger_type: event.target.value as WorkflowTemplate["trigger_type"],
                              trigger_config:
                                event.target.value === "scheduled"
                                  ? {
                                      schedule_type: "daily",
                                      schedule_time: "08:00",
                                    }
                                  : current.trigger_config,
                            }))
                          }
                          className={INPUT_CLASS}
                          style={inputStyle()}
                        >
                          <option value="manual">manual</option>
                          <option value="scheduled">scheduled</option>
                          <option value="event">event</option>
                        </select>
                      </div>

                      {draft.trigger_type === "scheduled" && (
                        <>
                          <div>
                            <FieldLabel>Schedule-Typ</FieldLabel>
                            <select
                              value={String(draft.trigger_config.schedule_type ?? "daily")}
                              onChange={(event) =>
                                updateDraft((current) => ({
                                  ...current,
                                  trigger_config:
                                    event.target.value === "interval"
                                      ? {
                                          schedule_type: "interval",
                                          schedule_interval_hours: 24,
                                        }
                                      : event.target.value === "weekly"
                                        ? {
                                            schedule_type: "weekly",
                                            schedule_day: "mon",
                                            schedule_time: "08:30",
                                          }
                                        : {
                                            schedule_type: event.target.value,
                                            schedule_time: "08:00",
                                          },
                                }))
                              }
                              className={INPUT_CLASS}
                              style={inputStyle()}
                            >
                              <option value="daily">daily</option>
                              <option value="weekdays">weekdays</option>
                              <option value="weekly">weekly</option>
                              <option value="interval">interval</option>
                            </select>
                          </div>

                          {draft.trigger_config.schedule_type === "interval" ? (
                            <div>
                              <FieldLabel>Intervall (Stunden)</FieldLabel>
                              <input
                                type="number"
                                min={1}
                                value={Number(draft.trigger_config.schedule_interval_hours ?? 24)}
                                onChange={(event) =>
                                  updateDraft((current) => ({
                                    ...current,
                                    trigger_config: {
                                      ...current.trigger_config,
                                      schedule_interval_hours: Number(event.target.value) || 24,
                                    },
                                  }))
                                }
                                className={INPUT_CLASS}
                                style={inputStyle()}
                              />
                            </div>
                          ) : draft.trigger_config.schedule_type === "weekly" ? (
                            <>
                              <div>
                                <FieldLabel>Weekday</FieldLabel>
                                <select
                                  value={String(draft.trigger_config.schedule_day ?? "mon")}
                                  onChange={(event) =>
                                    updateDraft((current) => ({
                                      ...current,
                                      trigger_config: {
                                        ...current.trigger_config,
                                        schedule_day: event.target.value,
                                      },
                                    }))
                                  }
                                  className={INPUT_CLASS}
                                  style={inputStyle()}
                                >
                                  {WEEKDAY_OPTIONS.map((option) => (
                                    <option key={option.value} value={option.value}>
                                      {option.label}
                                    </option>
                                  ))}
                                </select>
                              </div>

                              <div>
                                <FieldLabel>Time</FieldLabel>
                                <input
                                  type="time"
                                  value={String(draft.trigger_config.schedule_time ?? "08:30")}
                                  onChange={(event) =>
                                    updateDraft((current) => ({
                                      ...current,
                                      trigger_config: {
                                        ...current.trigger_config,
                                        schedule_time: event.target.value,
                                      },
                                    }))
                                  }
                                  className={INPUT_CLASS}
                                  style={inputStyle()}
                                />
                              </div>
                            </>
                          ) : (
                            <div>
                              <FieldLabel>Time</FieldLabel>
                              <input
                                type="time"
                                value={String(draft.trigger_config.schedule_time ?? "08:00")}
                                onChange={(event) =>
                                  updateDraft((current) => ({
                                    ...current,
                                    trigger_config: {
                                      ...current.trigger_config,
                                      schedule_time: event.target.value,
                                    },
                                  }))
                                }
                                className={INPUT_CLASS}
                                style={inputStyle()}
                              />
                            </div>
                          )}
                        </>
                      )}
                    </div>
                  </InlineSection>
                </div>

                <div className="space-y-6">
                  <InlineSection
                    title="Delivery"
                    description="Discord delivery stays in the same surface and no longer feels like a separate section."
                  >
                    <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-1">
                      <div>
                        <FieldLabel>Delivery Mode</FieldLabel>
                        <select
                          value={draft.delivery_mode}
                          onChange={(event) =>
                            updateDraft((current) => ({
                              ...current,
                              delivery_mode: event.target.value as WorkflowDraft["delivery_mode"],
                              delivery_channel_id:
                                event.target.value === "discord_channel"
                                  ? current.delivery_channel_id
                                  : "",
                              delivery_channel_name:
                                event.target.value === "discord_channel"
                                  ? current.delivery_channel_name
                                  : "",
                            }))
                          }
                          className={INPUT_CLASS}
                          style={inputStyle()}
                        >
                          <option value="none">none</option>
                          <option value="discord_channel">discord_channel</option>
                        </select>
                      </div>

                      <div>
                        <FieldLabel>Deliver On</FieldLabel>
                        <select
                          value={draft.deliver_on}
                          onChange={(event) =>
                            updateDraft((current) => ({
                              ...current,
                              deliver_on: event.target.value as WorkflowDraft["deliver_on"],
                            }))
                          }
                          className={INPUT_CLASS}
                          style={inputStyle()}
                          disabled={draft.delivery_mode !== "discord_channel"}
                        >
                          <option value="success">success</option>
                          <option value="failure">failure</option>
                          <option value="always">always</option>
                        </select>
                      </div>

                      {/* Phase 31 / OCS-15: Gateway select removed — Discord guild is now
                          a singleton (settings.discord_guild_id), no per-workflow choice. */}

                      <div>
                        <FieldLabel>Discord-Channel</FieldLabel>
                        <select
                          value={draft.delivery_channel_id}
                          onChange={(event) => {
                            const channel = channels.find((item) => item.id === event.target.value);
                            updateDraft((current) => ({
                              ...current,
                              delivery_channel_id: event.target.value,
                              delivery_channel_name: channel?.name ?? "",
                            }));
                          }}
                          className={INPUT_CLASS}
                          style={inputStyle()}
                          disabled={
                            draft.delivery_mode !== "discord_channel" ||
                            channelsQuery.isLoading ||
                            Boolean(channelQueryError)
                          }
                        >
                          <option value="">{channelSelectPlaceholder}</option>
                          {channels.map((channel) => (
                            <option key={channel.id} value={channel.id}>
                              #{channel.name}
                            </option>
                          ))}
                        </select>
                        {draft.delivery_mode === "discord_channel" && channelsQuery.isLoading && (
                          <p
                            className="mt-2 text-xs leading-5"
                            style={{ color: "var(--color-text-muted)" }}
                          >
                            Loading Discord channels...
                          </p>
                        )}
                        {draft.delivery_mode === "discord_channel" && channelQueryError && (
                          <p
                            className="mt-2 text-xs leading-5"
                            style={{ color: "var(--color-status-error)" }}
                          >
                            {channelQueryError}
                          </p>
                        )}
                        {draft.delivery_mode === "discord_channel" &&
                          !channelsQuery.isLoading &&
                          !channelQueryError &&
                          !channels.length && (
                            <p
                              className="mt-2 text-xs leading-5"
                              style={{ color: "var(--color-text-muted)" }}
                            >
                              The Discord server is not returning any channels right now.
                            </p>
                          )}
                      </div>

                      <div className="md:col-span-2 xl:col-span-1">
                        <FieldLabel>Delivery Format</FieldLabel>
                        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-1">
                          {[
                            {
                              value: "summary_card",
                              title: "summary_card",
                              description: "Compact status block for repo checks, digests, and numeric results.",
                            },
                            {
                              value: "markdown",
                              title: "markdown",
                              description: "Freer-form text, good for briefings or LLM-generated summaries.",
                            },
                          ].map((option) => {
                            const active = draft.delivery_format === option.value;
                            return (
                              <button
                                key={option.value}
                                type="button"
                                onClick={() =>
                                  updateDraft((current) => ({
                                    ...current,
                                    delivery_format: option.value as WorkflowDraft["delivery_format"],
                                  }))
                                }
                                className="rounded-2xl p-4 text-left transition-colors"
                                style={{
                                  border: `1px solid ${active ? `${C.accent}52` : "rgba(255,255,255,0.08)"}`,
                                  backgroundColor: active
                                    ? `${C.accent}1A`
                                    : "rgba(255,255,255,0.03)",
                                }}
                                disabled={draft.delivery_mode !== "discord_channel"}
                              >
                                <div className="flex items-center gap-2 text-sm font-medium" style={{ color: "var(--color-text-primary)" }}>
                                  <Send size={15} />
                                  {option.title}
                                </div>
                                <div className="mt-2 text-sm leading-6" style={{ color: "var(--color-text-secondary)" }}>
                                  {option.description}
                                </div>
                              </button>
                            );
                          })}
                        </div>
                      </div>
                    </div>
                  </InlineSection>

                  <div className="h-px" style={{ backgroundColor: "rgba(255,255,255,0.08)" }} />

                  <InlineSection
                    title="Version note"
                    description="Optional: add a short note that will be attached the next time you create a version snapshot."
                  >
                    <input
                      value={changeReason}
                      onChange={(event) => setChangeReason(event.target.value)}
                      className={INPUT_CLASS}
                      style={inputStyle()}
                      placeholder="Optional: why this version matters"
                    />
                    <div
                      className="mt-3 rounded-2xl px-4 py-3 text-sm"
                      style={{
                        color: "var(--color-text-secondary)",
                        backgroundColor: "rgba(255,255,255,0.03)",
                        border: "1px solid rgba(255,255,255,0.08)",
                      }}
                    >
                      Save now updates the workflow in place. Use "Create version" when you want an explicit snapshot you can restore or delete later.
                    </div>
                  </InlineSection>
                </div>
              </div>
            </GlassCard>

            <GlassCard className="p-5">
              <div className="mb-5 flex flex-col gap-4 xl:flex-row xl:items-end xl:justify-between">
                <div>
                  <SectionTitle
                    eyebrow="Steps"
                    title="Workflow-Builder"
                    description="Order your steps on the left; edit only the currently selected step on the right."
                  />
                </div>
                <div className="flex flex-wrap gap-2">
                  {(["llm", "internal_api", "webhook", "script_ref"] as StepPresetKind[]).map((preset) => (
                    <button
                      key={preset}
                      type="button"
                      onClick={() => addStep(preset)}
                      className="inline-flex items-center gap-2 rounded-xl px-3 py-2 text-sm transition-colors"
                      style={{
                        color: "var(--color-text-primary)",
                        backgroundColor: "rgba(255,255,255,0.03)",
                        border: "1px solid rgba(255,255,255,0.08)",
                      }}
                    >
                      <Plus size={14} />
                      {stepPresetLabel(preset)}
                    </button>
                  ))}
                </div>
              </div>

              {draft.current_definition.steps.length === 0 ? (
                <div
                  className="rounded-2xl border border-dashed px-5 py-8 text-center"
                  style={{
                    borderColor: "rgba(255,255,255,0.12)",
                    color: "var(--color-text-secondary)",
                  }}
                >
                  Noch keine Steps definiert. Lege oben den ersten LLM-, API-, Webhook- oder Script-Step an.
                </div>
              ) : (
                <div className="grid gap-5 xl:grid-cols-[320px_minmax(0,1fr)]">
                  <div className="space-y-3">
                    {draft.current_definition.steps.map((step, index) => {
                      const isActive = index === selectedStepIndex;
                      return (
                        <div
                          key={`${step.key}-${index}`}
                          className="rounded-2xl border p-4 transition-colors"
                          style={{
                            borderColor: isActive ? `${C.accent}52` : "rgba(255,255,255,0.08)",
                            backgroundColor: isActive ? `${C.accent}14` : "rgba(255,255,255,0.02)",
                          }}
                        >
                          <button
                            type="button"
                            onClick={() => setSelectedStepIndex(index)}
                            className="w-full text-left"
                          >
                            <div className="flex items-start justify-between gap-3">
                              <div className="min-w-0">
                                <div className="flex items-center gap-2">
                                  <span
                                    className="inline-flex items-center rounded-full px-2 py-1 text-[11px] font-medium"
                                    style={{
                                      color: step.step_type === "llm" ? C.accent : "var(--color-text-primary)",
                                      border: "1px solid rgba(255,255,255,0.08)",
                                      backgroundColor:
                                        step.step_type === "llm"
                                          ? `${C.accent}1A`
                                          : "rgba(255,255,255,0.03)",
                                    }}
                                  >
                                    {step.step_type === "llm" ? "LLM" : step.executor_type ?? "API"}
                                  </span>
                                  <span className="text-xs" style={{ color: "var(--color-text-muted)" }}>
                                    Schritt {index + 1}
                                  </span>
                                </div>
                                <div
                                  className="mt-2 truncate text-sm font-medium"
                                  style={{ color: "var(--color-text-primary)" }}
                                >
                                  {step.name || `Step ${index + 1}`}
                                </div>
                                <div className="mt-1 text-xs" style={{ color: "var(--color-text-muted)" }}>
                                  {step.key || "ohne_key"} • {describeStep(step, agents)}
                                </div>
                              </div>
                            </div>
                          </button>

                          <div className="mt-3 flex flex-wrap gap-2">
                            <button
                              type="button"
                              onClick={() => moveStep(index, -1)}
                              disabled={index === 0}
                              className="inline-flex items-center gap-2 rounded-xl px-3 py-2 text-xs transition-colors disabled:cursor-not-allowed disabled:opacity-40"
                              style={{
                                color: "var(--color-text-primary)",
                                backgroundColor: "rgba(255,255,255,0.03)",
                                border: "1px solid rgba(255,255,255,0.08)",
                              }}
                            >
                              <ChevronUp size={13} />
                              Hoch
                            </button>

                            <button
                              type="button"
                              onClick={() => moveStep(index, 1)}
                              disabled={index === draft.current_definition.steps.length - 1}
                              className="inline-flex items-center gap-2 rounded-xl px-3 py-2 text-xs transition-colors disabled:cursor-not-allowed disabled:opacity-40"
                              style={{
                                color: "var(--color-text-primary)",
                                backgroundColor: "rgba(255,255,255,0.03)",
                                border: "1px solid rgba(255,255,255,0.08)",
                              }}
                            >
                              <ChevronDown size={13} />
                              Runter
                            </button>

                            <button
                              type="button"
                              onClick={() => duplicateStep(index)}
                              className="inline-flex items-center gap-2 rounded-xl px-3 py-2 text-xs transition-colors"
                              style={{
                                color: "var(--color-text-primary)",
                                backgroundColor: "rgba(255,255,255,0.03)",
                                border: "1px solid rgba(255,255,255,0.08)",
                              }}
                            >
                              <Copy size={13} />
                              Duplizieren
                            </button>

                            <button
                              type="button"
                              onClick={() => removeStep(index)}
                              className="inline-flex items-center gap-2 rounded-xl px-3 py-2 text-xs transition-colors"
                              style={{
                                color: "var(--color-status-error)",
                                backgroundColor: "rgba(239,68,68,0.08)",
                                border: "1px solid rgba(239,68,68,0.18)",
                              }}
                            >
                              <Trash2 size={13} />
                              Entfernen
                            </button>
                          </div>
                        </div>
                      );
                    })}
                  </div>

                  {selectedStep && (
                    <div
                      className="rounded-2xl border p-5"
                      style={{
                        borderColor: "rgba(255,255,255,0.08)",
                        backgroundColor: "rgba(255,255,255,0.02)",
                      }}
                    >
                      <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
                        <div>
                          <div className="text-xs uppercase tracking-[0.18em]" style={{ color: C.accent }}>
                            Schritt {selectedStepIndex + 1}
                          </div>
                          <h3 className="mt-2 text-xl font-semibold" style={{ color: "var(--color-text-primary)" }}>
                            {selectedStep.name || `Step ${selectedStepIndex + 1}`}
                          </h3>
                          <p className="mt-1 text-sm" style={{ color: "var(--color-text-secondary)" }}>
                            {describeStep(selectedStep, agents)}
                          </p>
                        </div>
                        <span
                          className="inline-flex items-center rounded-full px-2.5 py-1 text-[11px] font-medium"
                          style={{
                            color: selectedStep.step_type === "llm" ? C.accent : "var(--color-text-primary)",
                            border: "1px solid rgba(255,255,255,0.08)",
                            backgroundColor:
                              selectedStep.step_type === "llm"
                                ? `${C.accent}1A`
                                : "rgba(255,255,255,0.03)",
                          }}
                        >
                          {selectedStep.step_type === "llm" ? "LLM Step" : "Deterministic Step"}
                        </span>
                      </div>

                      <div className="mt-5 grid gap-4 md:grid-cols-2">
                        <div>
                          <FieldLabel>Name</FieldLabel>
                          <input
                            value={selectedStep.name}
                            onChange={(event) =>
                              updateStep(selectedStepIndex, (current) => ({
                                ...current,
                                name: event.target.value,
                              }))
                            }
                            className={INPUT_CLASS}
                            style={inputStyle()}
                            placeholder="Planning Digest"
                          />
                        </div>

                        <div>
                          <div className="mb-2 flex items-center justify-between gap-2">
                            <FieldLabel>Key</FieldLabel>
                            <button
                              type="button"
                              onClick={() =>
                                updateStep(selectedStepIndex, (current) => ({
                                  ...current,
                                  key: buildStepKeyCandidate(
                                    current.name,
                                    `step_${selectedStepIndex + 1}`
                                  ),
                                }))
                              }
                              className="text-xs"
                              style={{ color: C.accent }}
                            >
                              from name
                            </button>
                          </div>
                          <input
                            value={selectedStep.key}
                            onChange={(event) =>
                              updateStep(selectedStepIndex, (current) => ({
                                ...current,
                                key: event.target.value,
                              }))
                            }
                            className={INPUT_CLASS}
                            style={inputStyle()}
                            placeholder="planning_digest"
                          />
                        </div>

                        <div>
                          <FieldLabel>Step Type</FieldLabel>
                          <select
                            value={selectedStep.step_type}
                            onChange={(event) =>
                              updateStep(selectedStepIndex, (current) => {
                                const nextType = event.target.value as WorkflowDraftStep["step_type"];
                                if (nextType === "deterministic") {
                                  return {
                                    ...current,
                                    step_type: "deterministic",
                                    executor_type: current.executor_type ?? "internal_api",
                                    agent_id: "",
                                    executor_config_text:
                                      current.executor_config_text.trim() ||
                                      formatJson(defaultExecutorConfig(current.executor_type ?? "internal_api")),
                                  };
                                }
                                return {
                                  ...current,
                                  step_type: "llm",
                                  executor_type: null,
                                  agent_id:
                                    current.agent_id ||
                                    agents.find((agent) => agent.provision_status === "provisioned")?.id ||
                                    agents[0]?.id ||
                                    "",
                                };
                              })
                            }
                            className={INPUT_CLASS}
                            style={inputStyle()}
                          >
                            <option value="llm">llm</option>
                            <option value="deterministic">deterministic</option>
                          </select>
                        </div>

                        <div>
                          <FieldLabel>Output Type</FieldLabel>
                          <select
                            value={selectedStep.output_type}
                            onChange={(event) =>
                              updateStep(selectedStepIndex, (current) => ({
                                ...current,
                                output_type: event.target.value as WorkflowDraftStep["output_type"],
                              }))
                            }
                            className={INPUT_CLASS}
                            style={inputStyle()}
                          >
                            <option value="text">text</option>
                            <option value="json">json</option>
                          </select>
                        </div>

                        {selectedStep.step_type === "llm" ? (
                          <>
                            <div>
                              <FieldLabel>Agent</FieldLabel>
                              <select
                                value={selectedStep.agent_id}
                                onChange={(event) =>
                                  updateStep(selectedStepIndex, (current) => ({
                                    ...current,
                                    agent_id: event.target.value,
                                  }))
                                }
                                className={INPUT_CLASS}
                                style={inputStyle()}
                              >
                                <option value="">Choose agent</option>
                                {agents.map((agentOption) => (
                                  <option key={agentOption.id} value={agentOption.id}>
                                    {agentOption.name}
                                    {agentOption.provision_status === "provisioned" ? "" : " • not provisioned"}
                                  </option>
                                ))}
                              </select>
                            </div>

                            <div>
                              <FieldLabel>Skill Key</FieldLabel>
                              <input
                                value={selectedStep.skill_key}
                                onChange={(event) =>
                                  updateStep(selectedStepIndex, (current) => ({
                                    ...current,
                                    skill_key: event.target.value,
                                  }))
                                }
                                className={INPUT_CLASS}
                                style={inputStyle()}
                                placeholder="optional, z. B. briefing_writer"
                              />
                            </div>
                          </>
                        ) : (
                          <div className="md:col-span-2">
                            <FieldLabel>Executor Type</FieldLabel>
                            <select
                              value={selectedStep.executor_type ?? "internal_api"}
                              onChange={(event) =>
                                updateStep(selectedStepIndex, (current) => ({
                                  ...current,
                                  executor_type: event.target.value,
                                  executor_config_text:
                                    current.executor_config_text.trim() ||
                                    formatJson(defaultExecutorConfig(event.target.value)),
                                }))
                              }
                              className={INPUT_CLASS}
                              style={inputStyle()}
                            >
                              <option value="internal_api">internal_api</option>
                              <option value="webhook">webhook</option>
                              <option value="script_ref">script_ref</option>
                            </select>
                          </div>
                        )}

                        <div className="md:col-span-2">
                          <FieldLabel>Input Template</FieldLabel>
                          <textarea
                            value={selectedStep.input_template}
                            onChange={(event) =>
                              updateStep(selectedStepIndex, (current) => ({
                                ...current,
                                input_template: event.target.value,
                              }))
                            }
                            className={cn(INPUT_CLASS, "resize-y")}
                            style={inputStyle(true)}
                            placeholder="Use e.g. {{workflow.name}}, {{run.id}}, or {{steps.previous_step.output}}"
                          />
                        </div>
                      </div>

                      <details
                        className="mt-5 rounded-2xl border px-4 py-4"
                        style={{
                          borderColor: "rgba(255,255,255,0.08)",
                          backgroundColor: "rgba(255,255,255,0.02)",
                        }}
                      >
                        <summary className="cursor-pointer list-none">
                          <div className="flex items-center justify-between gap-3">
                            <div>
                              <div className="text-sm font-medium" style={{ color: "var(--color-text-primary)" }}>
                                Advanced settings
                              </div>
                              <div className="mt-1 text-xs" style={{ color: "var(--color-text-muted)" }}>
                                Timeout, retry, executor JSON, and optional evaluation rules
                              </div>
                            </div>
                          </div>
                        </summary>

                        <div className="mt-4 grid gap-4 md:grid-cols-2">
                          <div>
                            <FieldLabel>Timeout (seconds)</FieldLabel>
                            <input
                              type="number"
                              min={5}
                              max={7200}
                              value={selectedStep.timeout_seconds}
                              onChange={(event) =>
                                updateStep(selectedStepIndex, (current) => ({
                                  ...current,
                                  timeout_seconds: Number(event.target.value) || 300,
                                }))
                              }
                              className={INPUT_CLASS}
                              style={inputStyle()}
                            />
                          </div>

                          <div>
                            <FieldLabel>On Error</FieldLabel>
                            <select
                              value={selectedStep.on_error}
                              onChange={(event) =>
                                updateStep(selectedStepIndex, (current) => ({
                                  ...current,
                                  on_error: event.target.value as WorkflowDraftStep["on_error"],
                                }))
                              }
                              className={INPUT_CLASS}
                              style={inputStyle()}
                            >
                              <option value="abort">abort</option>
                              <option value="retry">retry</option>
                              <option value="skip">skip</option>
                            </select>
                          </div>

                          <div>
                            <FieldLabel>Retries</FieldLabel>
                            <input
                              type="number"
                              min={0}
                              max={5}
                              value={selectedStep.retry_max_attempts}
                              onChange={(event) =>
                                updateStep(selectedStepIndex, (current) => ({
                                  ...current,
                                  retry_max_attempts: Number(event.target.value) || 0,
                                }))
                              }
                              className={INPUT_CLASS}
                              style={inputStyle()}
                            />
                          </div>

                          <div>
                            <FieldLabel>Retry delay (seconds)</FieldLabel>
                            <input
                              type="number"
                              min={0}
                              max={3600}
                              value={selectedStep.retry_delay_seconds}
                              onChange={(event) =>
                                updateStep(selectedStepIndex, (current) => ({
                                  ...current,
                                  retry_delay_seconds: Number(event.target.value) || 0,
                                }))
                              }
                              className={INPUT_CLASS}
                              style={inputStyle()}
                            />
                          </div>

                          {selectedStep.step_type === "deterministic" && (
                            <div className="md:col-span-2">
                              <FieldLabel>Executor Config (JSON)</FieldLabel>
                              <textarea
                                value={selectedStep.executor_config_text}
                                onChange={(event) =>
                                  updateStep(selectedStepIndex, (current) => ({
                                    ...current,
                                    executor_config_text: event.target.value,
                                  }))
                                }
                                className={cn(INPUT_CLASS, "resize-y font-mono text-[12px] leading-6")}
                                style={inputStyle(true)}
                                placeholder={formatJson(defaultExecutorConfig(selectedStep.executor_type ?? "internal_api"))}
                              />
                            </div>
                          )}

                          <div className="md:col-span-2">
                            <FieldLabel>Evaluation Contract (optional JSON)</FieldLabel>
                            <textarea
                              value={selectedStep.evaluation_contract_text}
                              onChange={(event) =>
                                updateStep(selectedStepIndex, (current) => ({
                                  ...current,
                                  evaluation_contract_text: event.target.value,
                                }))
                              }
                              className={cn(INPUT_CLASS, "resize-y font-mono text-[12px] leading-6")}
                              style={inputStyle(true)}
                              placeholder='{"type":"json_schema","schema":{"type":"object"}}'
                            />
                          </div>
                        </div>
                      </details>
                    </div>
                  )}
                </div>
              )}
            </GlassCard>
              </>
            )}
          </div>
        ) : (
          <div className="space-y-6">
            <GlassCard className="p-5">
              <SectionTitle
                eyebrow="Runs"
                title="Run-History"
                description="Running and past executions, including delivery status and step outputs."
              />

              {activeRun && (
                <div
                  className="mb-4 rounded-2xl border px-4 py-3"
                  style={{
                    borderColor: RUN_STATUS_META[activeRun.status].border,
                    backgroundColor: RUN_STATUS_META[activeRun.status].bg,
                  }}
                >
                  <div className="flex items-center justify-between gap-3">
                    <div>
                      <div className="text-sm font-medium" style={{ color: "var(--color-text-primary)" }}>
                        Aktiver Run
                      </div>
                      <div className="mt-1 text-xs" style={{ color: "var(--color-text-muted)" }}>
                        {formatDateTime(activeRun.started_at)} • {activeRun.current_step_key || "initialisiert"}
                      </div>
                    </div>
                    <div className="flex gap-2">
                      {activeRun.status === "running" && (
                        <button
                          type="button"
                          onClick={() => pauseMutation.mutate(activeRun.id)}
                          disabled={pauseMutation.isPending}
                          className="inline-flex items-center gap-2 rounded-xl px-3 py-2 text-sm"
                          style={{
                            color: C.warning,
                            backgroundColor: "rgba(184,135,10,0.1)",
                            border: "1px solid rgba(184,135,10,0.22)",
                          }}
                        >
                          <Pause size={14} />
                          Pause
                        </button>
                      )}
                      {activeRun.status === "paused" && (
                        <button
                          type="button"
                          onClick={() => resumeMutation.mutate(activeRun.id)}
                          disabled={resumeMutation.isPending}
                          className="inline-flex items-center gap-2 rounded-xl px-3 py-2 text-sm"
                          style={{
                            color: C.accent,
                            backgroundColor: `${C.accent}1A`,
                            border: `1px solid ${C.accent}38`,
                          }}
                        >
                          <Play size={14} />
                          Resume
                        </button>
                      )}
                      <button
                        type="button"
                        onClick={() => stopMutation.mutate(activeRun.id)}
                        disabled={stopMutation.isPending}
                        className="inline-flex items-center gap-2 rounded-xl px-3 py-2 text-sm"
                        style={{
                          color: "var(--color-status-error)",
                          backgroundColor: "rgba(239,68,68,0.08)",
                          border: "1px solid rgba(239,68,68,0.18)",
                        }}
                      >
                        <Square size={14} />
                        Stop
                      </button>
                    </div>
                  </div>
                </div>
              )}

              <div className="space-y-3">
                {runsQuery.isLoading ? (
                  <div className="flex items-center gap-3 text-sm" style={{ color: "var(--color-text-secondary)" }}>
                    <RefreshCw size={15} className="animate-spin" />
                    Loading runs...
                  </div>
                ) : runs.length === 0 ? (
                  <div className="text-sm" style={{ color: "var(--color-text-secondary)" }}>
                    Noch kein Run gestartet.
                  </div>
                ) : (
                  runs.map((run) => {
                    const status = RUN_STATUS_META[run.status];
                    const isSelected = run.id === selectedRunId;
                    return (
                      <button
                        key={run.id}
                        type="button"
                        onClick={() => setSelectedRunId(run.id)}
                        className="w-full rounded-2xl border px-4 py-3 text-left transition-colors"
                        style={{
                          borderColor: isSelected ? C.accent : "rgba(255,255,255,0.08)",
                          backgroundColor: isSelected
                            ? `${C.accent}14`
                            : "rgba(255,255,255,0.02)",
                        }}
                      >
                        <div className="flex items-start justify-between gap-3">
                          <div>
                            <div className="text-sm font-medium" style={{ color: "var(--color-text-primary)" }}>
                              Run {run.id.slice(0, 8)}
                            </div>
                            <div className="mt-1 text-xs" style={{ color: "var(--color-text-muted)" }}>
                              {formatDateTime(run.started_at)} • v{run.workflow_version}
                            </div>
                          </div>
                          <span
                            className="inline-flex items-center rounded-full px-2 py-1 text-[11px] font-medium"
                            style={{
                              color: status.text,
                              border: `1px solid ${status.border}`,
                              backgroundColor: status.bg,
                            }}
                          >
                            {status.label}
                          </span>
                        </div>
                        <div className="mt-2 flex flex-wrap gap-3 text-xs" style={{ color: "var(--color-text-muted)" }}>
                          <span className="inline-flex items-center gap-1.5">
                            <Clock3 size={12} />
                            {run.completed_at ? `Done ${timeAgo(run.completed_at)}` : "running"}
                          </span>
                          <span className="inline-flex items-center gap-1.5">
                            <History size={12} />
                            Delivery {run.delivery_status ?? "—"}
                          </span>
                        </div>
                      </button>
                    );
                  })
                )}
              </div>
            </GlassCard>

            <GlassCard className="p-5">
              <SectionTitle
                eyebrow="Run Detail"
                title="Steps and outputs"
                description="Ideal for debugging: rendered input, stdout/stderr, HTTP status, and structured outputs."
              />

              {runDetailQuery.isLoading ? (
                <div className="flex items-center gap-3 text-sm" style={{ color: "var(--color-text-secondary)" }}>
                  <RefreshCw size={15} className="animate-spin" />
                  Loading run detail...
                </div>
              ) : !runDetailQuery.data ? (
                <div className="text-sm" style={{ color: "var(--color-text-secondary)" }}>
                  Select a run to see the step details.
                </div>
              ) : (
                <div className="space-y-4">
                  <div
                    className="rounded-2xl border px-4 py-4"
                    style={{
                      borderColor: "rgba(255,255,255,0.08)",
                      backgroundColor: "rgba(255,255,255,0.02)",
                    }}
                  >
                    <div className="flex items-center justify-between gap-3">
                      <div>
                        <div className="text-sm font-medium" style={{ color: "var(--color-text-primary)" }}>
                          {selectedRun ? `Run ${selectedRun.id.slice(0, 8)}` : "Run"}
                        </div>
                        <div className="mt-1 text-xs" style={{ color: "var(--color-text-muted)" }}>
                          {selectedRun ? `${formatDateTime(selectedRun.started_at)} • ${selectedRun.delivery_status ?? "—"}` : "—"}
                        </div>
                      </div>
                      {selectedRun && (
                        <span
                          className="inline-flex items-center rounded-full px-2 py-1 text-[11px] font-medium"
                          style={{
                            color: RUN_STATUS_META[selectedRun.status].text,
                            border: `1px solid ${RUN_STATUS_META[selectedRun.status].border}`,
                            backgroundColor: RUN_STATUS_META[selectedRun.status].bg,
                          }}
                        >
                          {RUN_STATUS_META[selectedRun.status].label}
                        </span>
                      )}
                    </div>
                  </div>

                  {runDetailQuery.data.steps.length === 0 ? (
                    <div className="text-sm" style={{ color: "var(--color-text-secondary)" }}>
                      Dieser Run hat noch keine Step-Rows.
                    </div>
                  ) : (
                    runDetailQuery.data.steps.map((step) => {
                      const status = STEP_STATUS_META[step.status];
                      return (
                        <details
                          key={step.id}
                          className="rounded-2xl border px-4 py-4"
                          style={{
                            borderColor: "rgba(255,255,255,0.08)",
                            backgroundColor: "rgba(255,255,255,0.02)",
                          }}
                          open={step.status === "running" || step.status === "failed"}
                        >
                          <summary className="cursor-pointer list-none">
                            <div className="flex items-start justify-between gap-3">
                              <div>
                                <div className="flex items-center gap-2">
                                  <span className="text-sm font-medium" style={{ color: "var(--color-text-primary)" }}>
                                    {step.step_name}
                                  </span>
                                  <span className="text-xs" style={{ color: "var(--color-text-muted)" }}>
                                    {step.step_key}
                                  </span>
                                </div>
                                <div className="mt-1 text-xs" style={{ color: "var(--color-text-muted)" }}>
                                  {step.step_type} • {step.executor_type ?? "llm"} • Versuch {step.attempt}
                                </div>
                              </div>
                              <span
                                className="inline-flex items-center rounded-full px-2 py-1 text-[11px] font-medium"
                                style={{
                                  color: status.text,
                                  border: `1px solid ${status.border}`,
                                  backgroundColor: status.bg,
                                }}
                              >
                                {status.label}
                              </span>
                            </div>
                          </summary>

                          <div className="mt-4 space-y-4 text-sm">
                            {step.rendered_input && (
                              <div>
                                <FieldLabel>Rendered Input</FieldLabel>
                                <pre
                                  className="overflow-x-auto rounded-xl border p-3 text-[12px] leading-6"
                                  style={{
                                    color: "var(--color-text-primary)",
                                    borderColor: "rgba(255,255,255,0.08)",
                                    backgroundColor: "rgba(0,0,0,0.16)",
                                  }}
                                >
                                  {step.rendered_input}
                                </pre>
                              </div>
                            )}

                            {step.output_text && (
                              <div>
                                <FieldLabel>Output Text</FieldLabel>
                                <pre
                                  className="overflow-x-auto rounded-xl border p-3 text-[12px] leading-6"
                                  style={{
                                    color: "var(--color-text-primary)",
                                    borderColor: "rgba(255,255,255,0.08)",
                                    backgroundColor: "rgba(0,0,0,0.16)",
                                  }}
                                >
                                  {step.output_text}
                                </pre>
                              </div>
                            )}

                            {step.output_json != null && (
                              <div>
                                <FieldLabel>Output JSON</FieldLabel>
                                <pre
                                  className="overflow-x-auto rounded-xl border p-3 text-[12px] leading-6"
                                  style={{
                                    color: "var(--color-text-primary)",
                                    borderColor: "rgba(255,255,255,0.08)",
                                    backgroundColor: "rgba(0,0,0,0.16)",
                                  }}
                                >
                                  {formatJson(step.output_json)}
                                </pre>
                              </div>
                            )}

                            {(step.stdout || step.stderr) && (
                              <div className="grid gap-4">
                                {step.stdout && (
                                  <div>
                                    <FieldLabel>stdout</FieldLabel>
                                    <pre
                                      className="overflow-x-auto rounded-xl border p-3 text-[12px] leading-6"
                                      style={{
                                        color: "var(--color-text-primary)",
                                        borderColor: "rgba(255,255,255,0.08)",
                                        backgroundColor: "rgba(0,0,0,0.16)",
                                      }}
                                    >
                                      {step.stdout}
                                    </pre>
                                  </div>
                                )}
                                {step.stderr && (
                                  <div>
                                    <FieldLabel>stderr</FieldLabel>
                                    <pre
                                      className="overflow-x-auto rounded-xl border p-3 text-[12px] leading-6"
                                      style={{
                                        color: "var(--color-status-error)",
                                        borderColor: "rgba(239,68,68,0.18)",
                                        backgroundColor: "rgba(64,8,8,0.25)",
                                      }}
                                    >
                                      {step.stderr}
                                    </pre>
                                  </div>
                                )}
                              </div>
                            )}

                            <div className="grid gap-3 md:grid-cols-2">
                              <div
                                className="rounded-xl border px-3 py-3 text-xs"
                                style={{
                                  color: "var(--color-text-secondary)",
                                  borderColor: "rgba(255,255,255,0.08)",
                                  backgroundColor: "rgba(255,255,255,0.02)",
                                }}
                              >
                                <div>Start: {formatDateTime(step.started_at)}</div>
                                <div className="mt-1">Ende: {formatDateTime(step.completed_at)}</div>
                              </div>
                              <div
                                className="rounded-xl border px-3 py-3 text-xs"
                                style={{
                                  color: "var(--color-text-secondary)",
                                  borderColor: "rgba(255,255,255,0.08)",
                                  backgroundColor: "rgba(255,255,255,0.02)",
                                }}
                              >
                                <div>HTTP: {step.http_status ?? "—"}</div>
                                <div className="mt-1">Exit Code: {step.exit_code ?? "—"}</div>
                                {step.error_message && (
                                  <div className="mt-1" style={{ color: "var(--color-status-error)" }}>
                                    {step.error_message}
                                  </div>
                                )}
                              </div>
                            </div>
                          </div>
                        </details>
                      );
                    })
                  )}
                </div>
              )}
            </GlassCard>
          </div>
        )}
      </div>
    </AppShell>
  );
}
