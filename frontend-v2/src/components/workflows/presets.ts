import type { Agent, WorkflowTemplate } from "@/lib/types";

export const WEEKDAY_OPTIONS = [
  { value: "mon", label: "Monday" },
  { value: "tue", label: "Tuesday" },
  { value: "wed", label: "Wednesday" },
  { value: "thu", label: "Thursday" },
  { value: "fri", label: "Friday" },
  { value: "sat", label: "Saturday" },
  { value: "sun", label: "Sunday" },
] as const;

export function weekdayLabel(day: string): string {
  return WEEKDAY_OPTIONS.find((option) => option.value === day)?.label ?? day;
}

export function formatWorkflowTriggerLabel(
  triggerType: WorkflowTemplate["trigger_type"],
  triggerConfig: Record<string, unknown> | null | undefined,
): string {
  if (triggerType === "manual") return "Manual";
  if (triggerType === "event") return "Event";

  const config = triggerConfig ?? {};
  const scheduleType = String(config.schedule_type ?? "");
  if (scheduleType === "interval") {
    const interval = Number(config.schedule_interval_hours ?? 0);
    return interval > 0 ? `Every ${interval}h` : "Interval";
  }
  if (scheduleType === "weekdays") {
    return config.schedule_time ? `Weekdays ${String(config.schedule_time)}` : "Weekdays";
  }
  if (scheduleType === "weekly") {
    const day = weekdayLabel(String(config.schedule_day ?? "mon"));
    return config.schedule_time
      ? `${day} ${String(config.schedule_time)}`
      : `Weekly ${day}`;
  }
  return config.schedule_time ? `Daily ${String(config.schedule_time)}` : "Daily";
}

function aiNewsAgentScore(agent: Agent): number {
  // Phase 31 / OCS-15: gateway_agent_id eligibility gate removed. Any agent
  // with a provisioned runtime (provision_status === "provisioned") can run
  // AI-News workflows now that Discord routing is per-agent, not per-gateway.
  if (agent.provision_status !== "provisioned") return -1;

  const tags = [...(agent.skills ?? []), ...(agent.skill_filter ?? [])].map((item) =>
    String(item).toLowerCase()
  );
  const role = String(agent.role ?? "").toLowerCase();
  const name = String(agent.name ?? "").toLowerCase();

  let score = 0;
  if (role.includes("research")) score += 5;
  if (name.includes("research")) score += 4;
  if (tags.some((tag) => tag.includes("search") || tag.includes("browser") || tag.includes("research"))) {
    score += 3;
  }
  if (tags.some((tag) => tag.includes("news") || tag.includes("web"))) {
    score += 1;
  }
  return score;
}

export function selectAiNewsAgents(agents: Agent[]): Agent[] {
  return [...agents]
    .filter((agent) => agent.provision_status === "provisioned")
    .sort((left, right) => {
      const scoreDiff = aiNewsAgentScore(right) - aiNewsAgentScore(left);
      if (scoreDiff !== 0) return scoreDiff;
      return left.name.localeCompare(right.name);
    });
}

export function buildWeeklyPlanningDigestPreset({
  boardId,
  boardName,
  agentId,
  scheduleDay,
  scheduleTime,
}: {
  boardId: string;
  boardName: string;
  agentId: string;
  scheduleDay: string;
  scheduleTime: string;
}) {
  return {
    name: `Weekly Planning Digest${boardName ? ` - ${boardName}` : ""}`,
    description:
      "Sammelt Board-Snapshot plus Activity-Feed und formuliert daraus einen kompakten Weekly Planning Digest in Markdown.",
    board_id: boardId,
    project_id: null,
    trigger_type: "scheduled" as const,
    trigger_config: {
      schedule_type: "weekly",
      schedule_day: scheduleDay,
      schedule_time: scheduleTime,
    },
    status: "draft" as const,
    max_runtime_minutes: 45,
    policy_profile: "safe",
    reflect_on: "manual",
    change_reason: "Starter preset: Weekly Planning Digest",
    current_definition: {
      steps: [
        {
          key: "board_snapshot",
          name: "Collect board snapshot",
          step_type: "deterministic" as const,
          execution_mode: "single" as const,
          output_type: "json" as const,
          timeout_seconds: 60,
          on_error: "abort" as const,
          retry_max_attempts: 1,
          retry_delay_seconds: 5,
          retry_backoff: "linear" as const,
          executor_type: "internal_api",
          executor_config: {
            method: "GET",
            path: "/api/v1/boards/{{workflow.board_id}}/snapshot",
          },
        },
        {
          key: "recent_activity",
          name: "Collect recent activity",
          step_type: "deterministic" as const,
          execution_mode: "single" as const,
          output_type: "json" as const,
          timeout_seconds: 60,
          on_error: "abort" as const,
          retry_max_attempts: 1,
          retry_delay_seconds: 5,
          retry_backoff: "linear" as const,
          executor_type: "internal_api",
          executor_config: {
            method: "GET",
            path: "/api/v1/activity?board_id={{workflow.board_id}}&limit=20",
          },
        },
        {
          key: "digest_writer",
          name: "Write weekly digest",
          step_type: "llm" as const,
          execution_mode: "single" as const,
          output_type: "text" as const,
          timeout_seconds: 600,
          on_error: "abort" as const,
          retry_max_attempts: 0,
          retry_delay_seconds: 0,
          retry_backoff: "linear" as const,
          agent_id: agentId,
          input_template: [
            'Du schreibst einen Weekly Planning Digest fuer das Mission-Control-Board "{{steps.board_snapshot.output.board.name}}".',
            "",
            "Arbeite konkret, handlungsorientiert und knapp. Nutze Task- oder Projekt-Namen, wenn sie in den Daten vorkommen.",
            "",
            "Board Snapshot JSON:",
            "{{steps.board_snapshot.output_text}}",
            "",
            "Recent Activity JSON:",
            "{{steps.recent_activity.output_text}}",
            "",
            "Bitte antworte in Markdown mit genau diesen Abschnitten:",
            "# Weekly Planning Digest",
            "## Gesamtbild",
            "## Erfolge",
            "## Risiken & Blocker",
            "## Fokus fuer naechste Woche",
            "## Offene Entscheidungen",
          ].join("\n"),
        },
      ],
    },
  };
}

export function buildAiNewsBriefingPreset({
  agentId,
  scheduleTime,
}: {
  agentId: string;
  scheduleTime: string;
}) {
  return {
    name: "AI News Briefing",
    description:
      "Daily AI news briefing with fact-checking guidance, impact notes and an optional OpenClaw Corner.",
    board_id: null,
    project_id: null,
    trigger_type: "scheduled" as const,
    trigger_config: {
      schedule_type: "weekdays",
      schedule_time: scheduleTime,
    },
    status: "draft" as const,
    current_definition: { steps: [] },
    max_runtime_minutes: 45,
    policy_profile: "safe",
    reflect_on: "manual",
    change_reason: "Starter preset: AI News Briefing",
    execution_policy: {
      workflow_kind: "ai_news_briefing",
      guided_config: {
        agent_id: agentId,
        topic_focus: "",
        custom_instructions:
          "Find the 5-7 most important AI news items from the last 24 hours. Use Discord-friendly markdown, include brief impact notes, prefer primary sources, and add an OpenClaw Corner with 1-3 useful highlights.",
        timeframe_hours: 24,
        max_items: 7,
        source_profile: "balanced",
        fact_check_level: "strict",
        include_impacts: true,
        include_emojis: true,
        include_openclaw_corner: true,
        openclaw_items: 2,
      },
    },
    delivery_config: {
      delivery_mode: "none",
      deliver_on: "success",
      delivery_format: "markdown",
    },
  };
}
