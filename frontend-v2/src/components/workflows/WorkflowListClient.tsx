"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowRight,
  CalendarClock,
  GitBranch,
  PlayCircle,
  Plus,
  RefreshCw,
  Send,
} from "lucide-react";
import AppShell from "@/components/layout/AppShell";
import { GlassCard } from "@/components/shared/GlassCard";
import { api } from "@/lib/api";
import { notify } from "@/lib/notify";
import { C } from "@/lib/colors";
import type { Agent, WorkflowTemplate } from "@/lib/types";
import { timeAgo } from "@/lib/utils";
import {
  buildAiNewsBriefingPreset,
  buildWeeklyPlanningDigestPreset,
  formatWorkflowTriggerLabel,
  selectAiNewsAgents,
  WEEKDAY_OPTIONS,
} from "./presets";

const STATUS_META: Record<
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
    label: "Validiert",
    text: C.accent,
    border: `${C.accent}4D`,
    bg: `${C.accent}1A`,
  },
  active: {
    label: "Aktiv",
    text: "var(--color-status-online)",
    border: "rgba(0,204,136,0.28)",
    bg: "rgba(0,204,136,0.08)",
  },
  archived: {
    label: "Archiv",
    text: "var(--color-text-muted)",
    border: "rgba(255,255,255,0.08)",
    bg: "rgba(255,255,255,0.02)",
  },
};

function deliveryLabel(workflow: WorkflowTemplate): string {
  const config = workflow.delivery_config ?? {};
  if (config.delivery_mode === "discord_channel") {
    const channelName = String(config.channel_name ?? "").trim();
    return channelName ? `Discord • #${channelName}` : "Discord";
  }
  return "Keine Delivery";
}

function summarize(workflows: WorkflowTemplate[]) {
  const active = workflows.filter((workflow) => workflow.status === "active").length;
  const scheduled = workflows.filter((workflow) => workflow.trigger_type === "scheduled").length;
  const withDelivery = workflows.filter(
    (workflow) => workflow.delivery_config?.delivery_mode === "discord_channel"
  ).length;
  return { active, scheduled, withDelivery };
}

function formatDateTime(value: string | null): string {
  if (!value) return "Noch kein Termin";
  return new Date(value).toLocaleString("de-CH", {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function formatError(error: unknown): string {
  if (!(error instanceof Error)) return "Unbekannter Fehler";
  const raw = error.message.replace(/^API \d+:\s*/, "");
  try {
    const parsed = JSON.parse(raw) as { detail?: string };
    return parsed.detail ?? raw;
  } catch {
    return raw;
  }
}

export default function WorkflowListClient() {
  const router = useRouter();
  const queryClient = useQueryClient();
  const [starterBoardId, setStarterBoardId] = useState("");
  const [starterAgentId, setStarterAgentId] = useState("");
  const [starterScheduleDay, setStarterScheduleDay] = useState("mon");
  const [starterScheduleTime, setStarterScheduleTime] = useState("08:30");
  const [aiNewsAgentId, setAiNewsAgentId] = useState("");
  const [aiNewsScheduleTime, setAiNewsScheduleTime] = useState("08:00");

  const workflowsQuery = useQuery({
    queryKey: ["workflows"],
    queryFn: () => api.workflows.list(),
  });
  const boardsQuery = useQuery({
    queryKey: ["boards"],
    queryFn: () => api.boards.list(),
  });
  const agentsQuery = useQuery({
    queryKey: ["agents"],
    queryFn: () => api.agents.list(undefined, true),
  });

  const workflows = workflowsQuery.data ?? [];
  const boards = boardsQuery.data ?? [];
  const agents = agentsQuery.data ?? [];
  const stats = summarize(workflows);

  useEffect(() => {
    if (!starterBoardId && boards[0]) {
      setStarterBoardId(boards[0].id);
    }
  }, [boards, starterBoardId]);

  const starterAgents = agents.filter((agent) => {
    // Phase 31 / OCS-15: gateway_agent_id gate removed. Eligibility is now
    // provision_status === "provisioned" (any runtime, not just legacy gateway).
    const isProvisioned = agent.provision_status === "provisioned";
    if (!starterBoardId) return isProvisioned;
    return isProvisioned && (agent.board_id === starterBoardId || agent.board_id === null);
  });

  useEffect(() => {
    if (!starterAgents.length) {
      if (starterAgentId) setStarterAgentId("");
      return;
    }
    if (starterAgentId && starterAgents.some((agent) => agent.id === starterAgentId)) {
      return;
    }
    const preferred =
      starterAgents.find((agent) => agent.board_id === starterBoardId && agent.is_board_lead) ??
      starterAgents[0];
    setStarterAgentId(preferred?.id ?? "");
  }, [starterAgents, starterAgentId, starterBoardId]);

  const starterBoard = boards.find((board) => board.id === starterBoardId) ?? null;
  const aiNewsAgents = selectAiNewsAgents(agents);

  useEffect(() => {
    if (!aiNewsAgents.length) {
      if (aiNewsAgentId) setAiNewsAgentId("");
      return;
    }
    if (aiNewsAgentId && aiNewsAgents.some((agent) => agent.id === aiNewsAgentId)) {
      return;
    }
    const preferred = aiNewsAgents[0];
    setAiNewsAgentId(preferred?.id ?? "");
  }, [aiNewsAgents, aiNewsAgentId]);

  const createWorkflow = useMutation({
    mutationFn: () =>
      api.workflows.create({
        name: `Neuer Workflow ${new Date().toLocaleTimeString("de-CH", {
          hour: "2-digit",
          minute: "2-digit",
        })}`,
        description: null,
        trigger_type: "manual",
        status: "draft",
        current_definition: { steps: [] },
        max_runtime_minutes: 60,
        policy_profile: "safe",
        reflect_on: "manual",
      }),
    onSuccess: async (workflow) => {
      await queryClient.invalidateQueries({ queryKey: ["workflows"] });
      notify.success("Workflow angelegt");
      router.push(`/workflows/${workflow.id}`);
    },
    onError: (error) => notify.error(formatError(error)),
  });

  const createStarter = useMutation({
    mutationFn: () => {
      if (!starterBoard) {
        throw new Error("Bitte zuerst ein Board waehlen");
      }
      if (!starterAgentId) {
        throw new Error("Bitte zuerst einen provisionierten Agenten waehlen");
      }
      return api.workflows.create(
        buildWeeklyPlanningDigestPreset({
          boardId: starterBoard.id,
          boardName: starterBoard.name,
          agentId: starterAgentId,
          scheduleDay: starterScheduleDay,
          scheduleTime: starterScheduleTime,
        })
      );
    },
    onSuccess: async (workflow) => {
      await queryClient.invalidateQueries({ queryKey: ["workflows"] });
      notify.success("Weekly Planning Digest angelegt");
      router.push(`/workflows/${workflow.id}`);
    },
    onError: (error) => notify.error(formatError(error)),
  });

  const createAiNewsStarter = useMutation({
    mutationFn: () => {
      if (!aiNewsAgentId) {
        throw new Error("Please choose an agent first");
      }
      return api.workflows.create(
        buildAiNewsBriefingPreset({
          agentId: aiNewsAgentId,
          scheduleTime: aiNewsScheduleTime,
        })
      );
    },
    onSuccess: async (workflow) => {
      await queryClient.invalidateQueries({ queryKey: ["workflows"] });
      notify.success("AI News Briefing created");
      router.push(`/workflows/${workflow.id}`);
    },
    onError: (error) => notify.error(formatError(error)),
  });

  return (
    <AppShell>
      <div className="space-y-6">
        <div className="flex flex-col gap-4 lg:flex-row lg:items-end lg:justify-between">
          <div>
            <div
              className="inline-flex items-center gap-2 rounded-full px-3 py-1 text-[11px] uppercase tracking-[0.24em]"
              style={{
                color: C.accent,
                backgroundColor: C.accentSubtle,
                border: `1px solid ${C.borderAccent}`,
              }}
            >
              <GitBranch size={12} />
              Workflows
            </div>
            <h1
              className="mt-3 text-3xl font-semibold tracking-tight"
              style={{ color: "var(--color-text-primary)" }}
            >
              Wiederkehrende Abläufe zentral steuern
            </h1>
            <p
              className="mt-2 max-w-3xl text-sm leading-6"
              style={{ color: "var(--color-text-secondary)" }}
            >
              Hier legst du wiederkehrende Ergebnisse fest: Was soll passieren, wann soll es laufen und wohin soll das Resultat gehen.
            </p>
          </div>

          <div className="flex items-center gap-3">
            <button
              type="button"
              onClick={() => workflowsQuery.refetch()}
              className="inline-flex items-center gap-2 rounded-xl px-4 py-2 text-sm transition-colors"
              style={{
                color: "var(--color-text-secondary)",
                border: "1px solid rgba(255,255,255,0.08)",
                backgroundColor: "rgba(255,255,255,0.03)",
              }}
            >
              <RefreshCw
                size={15}
                className={workflowsQuery.isFetching ? "animate-spin" : undefined}
              />
              Aktualisieren
            </button>
            <button
              type="button"
              onClick={() => createWorkflow.mutate()}
              disabled={createWorkflow.isPending}
              className="inline-flex items-center gap-2 rounded-xl px-4 py-2 text-sm font-medium text-white transition-opacity disabled:cursor-not-allowed disabled:opacity-60"
              style={{
                background: `linear-gradient(135deg, ${C.accent}, ${C.accentHover})`,
              }}
            >
              {createWorkflow.isPending ? (
                <RefreshCw size={15} className="animate-spin" />
              ) : (
                <Plus size={15} />
              )}
              Leerer Workflow
            </button>
          </div>
        </div>

        <div className="grid gap-3 md:grid-cols-3">
          <GlassCard className="p-4">
            <div className="text-xs uppercase tracking-[0.18em]" style={{ color: "var(--color-text-muted)" }}>
              Aktiv
            </div>
            <div className="mt-2 text-3xl font-semibold" style={{ color: "var(--color-status-online)" }}>
              {stats.active}
            </div>
            <div className="mt-2 text-xs" style={{ color: "var(--color-text-muted)" }}>
              Von {workflows.length} Workflows gerade scharf geschaltet
            </div>
          </GlassCard>

          <GlassCard className="p-4">
            <div className="text-xs uppercase tracking-[0.18em]" style={{ color: "var(--color-text-muted)" }}>
              Geplant
            </div>
            <div className="mt-2 text-3xl font-semibold" style={{ color: C.accent }}>
              {stats.scheduled}
            </div>
            <div className="mt-2 text-xs" style={{ color: "var(--color-text-muted)" }}>
              Workflows mit taeglichem, woechentlichem, werktaeglichem oder Intervall-Trigger
            </div>
          </GlassCard>

          <GlassCard className="p-4">
            <div className="text-xs uppercase tracking-[0.18em]" style={{ color: "var(--color-text-muted)" }}>
              Delivery
            </div>
            <div className="mt-2 text-3xl font-semibold" style={{ color: "var(--color-text-primary)" }}>
              {stats.withDelivery}
            </div>
            <div className="mt-2 text-xs" style={{ color: "var(--color-text-muted)" }}>
              Ergebnisse werden an einen Discord-Channel ausgeliefert
            </div>
          </GlassCard>
        </div>

        <GlassCard className="p-5">
          <div className="flex flex-col gap-4 lg:flex-row lg:items-end lg:justify-between">
            <div className="max-w-2xl">
              <div className="text-[11px] uppercase tracking-[0.22em]" style={{ color: C.accent }}>
                Schnellstart
              </div>
              <h2 className="mt-2 text-xl font-semibold" style={{ color: "var(--color-text-primary)" }}>
                Woechentlicher Board-Digest
              </h2>
              <p className="mt-2 text-sm leading-6" style={{ color: "var(--color-text-secondary)" }}>
                Du waehlst nur Board, Agent und Zeitpunkt. MC baut dir den technischen Ablauf im Hintergrund und du kannst danach nur noch Delivery und Feintuning setzen.
              </p>
            </div>

            <button
              type="button"
              onClick={() => createStarter.mutate()}
              disabled={createStarter.isPending || !starterBoard || !starterAgentId}
              className="inline-flex items-center gap-2 rounded-xl px-4 py-2 text-sm font-medium text-white transition-opacity disabled:cursor-not-allowed disabled:opacity-60"
              style={{
                background: `linear-gradient(135deg, ${C.accent}, ${C.accentHover})`,
              }}
            >
              {createStarter.isPending ? (
                <RefreshCw size={15} className="animate-spin" />
              ) : (
                <Plus size={15} />
              )}
              Schnellstart anlegen
            </button>
          </div>

          <div className="mt-5 grid gap-4 md:grid-cols-2 xl:grid-cols-4">
            <div>
              <label className="mb-2 block text-xs uppercase tracking-[0.14em]" style={{ color: "var(--color-text-muted)" }}>
                Board
              </label>
              <select
                aria-label="Board"
                value={starterBoardId}
                onChange={(event) => setStarterBoardId(event.target.value)}
                className="w-full rounded-xl border px-3 py-2.5 text-sm"
                style={{
                  color: "var(--color-text-primary)",
                  backgroundColor: "rgba(255,255,255,0.03)",
                  borderColor: "rgba(255,255,255,0.08)",
                }}
              >
                <option value="">Board waehlen</option>
                {boards.map((board) => (
                  <option key={board.id} value={board.id}>
                    {board.name}
                  </option>
                ))}
              </select>
            </div>

            <div>
              <label className="mb-2 block text-xs uppercase tracking-[0.14em]" style={{ color: "var(--color-text-muted)" }}>
                Agent
              </label>
              <select
                aria-label="Agent"
                value={starterAgentId}
                onChange={(event) => setStarterAgentId(event.target.value)}
                className="w-full rounded-xl border px-3 py-2.5 text-sm"
                style={{
                  color: "var(--color-text-primary)",
                  backgroundColor: "rgba(255,255,255,0.03)",
                  borderColor: "rgba(255,255,255,0.08)",
                }}
              >
                <option value="">Agent waehlen</option>
                {starterAgents.map((agent: Agent) => (
                  <option key={agent.id} value={agent.id}>
                    {agent.name}
                    {agent.board_id === starterBoardId ? "" : " - global"}
                  </option>
                ))}
              </select>
            </div>

            <div>
              <label className="mb-2 block text-xs uppercase tracking-[0.14em]" style={{ color: "var(--color-text-muted)" }}>
                Wochentag
              </label>
              <select
                aria-label="Wochentag"
                value={starterScheduleDay}
                onChange={(event) => setStarterScheduleDay(event.target.value)}
                className="w-full rounded-xl border px-3 py-2.5 text-sm"
                style={{
                  color: "var(--color-text-primary)",
                  backgroundColor: "rgba(255,255,255,0.03)",
                  borderColor: "rgba(255,255,255,0.08)",
                }}
              >
                {WEEKDAY_OPTIONS.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
            </div>

            <div>
              <label className="mb-2 block text-xs uppercase tracking-[0.14em]" style={{ color: "var(--color-text-muted)" }}>
                Uhrzeit
              </label>
              <input
                type="time"
                aria-label="Uhrzeit"
                value={starterScheduleTime}
                onChange={(event) => setStarterScheduleTime(event.target.value)}
                className="w-full rounded-xl border px-3 py-2.5 text-sm"
                style={{
                  color: "var(--color-text-primary)",
                  backgroundColor: "rgba(255,255,255,0.03)",
                  borderColor: "rgba(255,255,255,0.08)",
                }}
              />
            </div>
          </div>
        </GlassCard>

        <GlassCard className="p-5">
          <div className="flex flex-col gap-4 lg:flex-row lg:items-end lg:justify-between">
            <div className="max-w-2xl">
              <div className="text-[11px] uppercase tracking-[0.22em]" style={{ color: C.accent }}>
                Guided Starter
              </div>
              <h2 className="mt-2 text-xl font-semibold" style={{ color: "var(--color-text-primary)" }}>
                AI News Briefing
              </h2>
              <p className="mt-2 text-sm leading-6" style={{ color: "var(--color-text-secondary)" }}>
                Tell MC which research-capable agent should prepare the briefing and when it should run. The workflow kind compiles the hidden steps in the backend and keeps the editor user-facing.
              </p>
            </div>

            <button
              type="button"
              onClick={() => createAiNewsStarter.mutate()}
              disabled={createAiNewsStarter.isPending || !aiNewsAgentId}
              className="inline-flex items-center gap-2 rounded-xl px-4 py-2 text-sm font-medium text-white transition-opacity disabled:cursor-not-allowed disabled:opacity-60"
              style={{
                background: `linear-gradient(135deg, ${C.accent}, ${C.accentHover})`,
              }}
            >
              {createAiNewsStarter.isPending ? (
                <RefreshCw size={15} className="animate-spin" />
              ) : (
                <Plus size={15} />
              )}
              Create AI News Briefing
            </button>
          </div>

          <div className="mt-5 grid gap-4 md:grid-cols-2 xl:grid-cols-3">
            <div>
              <label className="mb-2 block text-xs uppercase tracking-[0.14em]" style={{ color: "var(--color-text-muted)" }}>
                Briefing Agent
              </label>
              <select
                aria-label="Briefing Agent"
                value={aiNewsAgentId}
                onChange={(event) => setAiNewsAgentId(event.target.value)}
                className="w-full rounded-xl border px-3 py-2.5 text-sm"
                style={{
                  color: "var(--color-text-primary)",
                  backgroundColor: "rgba(255,255,255,0.03)",
                  borderColor: "rgba(255,255,255,0.08)",
                }}
              >
                <option value="">Choose agent</option>
                {aiNewsAgents.map((agent: Agent) => (
                  <option key={agent.id} value={agent.id}>
                    {agent.name}{agent.role ? ` • ${agent.role}` : ""}
                  </option>
                ))}
              </select>
              {aiNewsAgents.length > 0 && (
                <p className="mt-2 text-xs leading-5" style={{ color: "var(--color-text-muted)" }}>
                  Research-oriented agents are shown first, but any runnable gateway agent can be used.
                </p>
              )}
            </div>

            <div>
              <label className="mb-2 block text-xs uppercase tracking-[0.14em]" style={{ color: "var(--color-text-muted)" }}>
                Delivery Time
              </label>
              <input
                type="time"
                aria-label="Delivery Time"
                value={aiNewsScheduleTime}
                onChange={(event) => setAiNewsScheduleTime(event.target.value)}
                className="w-full rounded-xl border px-3 py-2.5 text-sm"
                style={{
                  color: "var(--color-text-primary)",
                  backgroundColor: "rgba(255,255,255,0.03)",
                  borderColor: "rgba(255,255,255,0.08)",
                }}
              />
            </div>

            <div
              className="rounded-2xl px-4 py-4 text-sm leading-6"
              style={{
                color: "var(--color-text-secondary)",
                backgroundColor: "rgba(255,255,255,0.03)",
                border: "1px solid rgba(255,255,255,0.08)",
              }}
            >
              Defaults: weekdays, last 24 hours, 5-7 items, strict fact-checking guidance, OpenClaw Corner enabled.
            </div>
          </div>
        </GlassCard>

        {workflowsQuery.isLoading ? (
          <GlassCard className="p-8">
            <div className="flex items-center gap-3 text-sm" style={{ color: "var(--color-text-secondary)" }}>
              <RefreshCw size={16} className="animate-spin" />
              Workflows werden geladen...
            </div>
          </GlassCard>
        ) : workflows.length === 0 ? (
          <GlassCard className="p-10">
            <div className="mx-auto max-w-xl text-center">
              <div
                className="mx-auto flex h-14 w-14 items-center justify-center rounded-2xl"
                style={{
                  backgroundColor: C.accentSubtle,
                  border: `1px solid ${C.borderAccent}`,
                  color: C.accent,
                }}
              >
                <GitBranch size={22} />
              </div>
              <h2 className="mt-4 text-xl font-semibold" style={{ color: "var(--color-text-primary)" }}>
                Noch keine Workflows angelegt
              </h2>
              <p className="mt-2 text-sm leading-6" style={{ color: "var(--color-text-secondary)" }}>
                Starte am besten mit dem Schnellstart oben. Falls du etwas ganz Freies bauen willst, kannst du auch mit einem leeren Workflow beginnen.
              </p>
              <button
                type="button"
                onClick={() => createWorkflow.mutate()}
                disabled={createWorkflow.isPending}
                className="mt-6 inline-flex items-center gap-2 rounded-xl px-4 py-2 text-sm font-medium text-white transition-opacity disabled:cursor-not-allowed disabled:opacity-60"
                style={{
                  background: `linear-gradient(135deg, ${C.accent}, ${C.accentHover})`,
                }}
              >
                {createWorkflow.isPending ? (
                  <RefreshCw size={15} className="animate-spin" />
                ) : (
                  <Plus size={15} />
                )}
                Leeren Workflow anlegen
              </button>
            </div>
          </GlassCard>
        ) : (
          <div className="grid gap-4 xl:grid-cols-2">
            {workflows.map((workflow) => {
              const statusMeta = STATUS_META[workflow.status];
              return (
                <Link key={workflow.id} href={`/workflows/${workflow.id}`} className="block">
                  <GlassCard className="h-full p-5 transition-transform hover:-translate-y-0.5">
                    <div className="flex items-start justify-between gap-4">
                      <div className="min-w-0">
                        <div className="flex items-center gap-2">
                          <h2
                            className="truncate text-lg font-semibold"
                            style={{ color: "var(--color-text-primary)" }}
                          >
                            {workflow.name}
                          </h2>
                          <span
                            className="inline-flex shrink-0 items-center rounded-full px-2 py-1 text-[11px] font-medium"
                            style={{
                              color: statusMeta.text,
                              border: `1px solid ${statusMeta.border}`,
                              backgroundColor: statusMeta.bg,
                            }}
                          >
                            {statusMeta.label}
                          </span>
                        </div>
                        {workflow.description && (
                          <p
                            className="mt-2 line-clamp-2 text-sm leading-6"
                            style={{ color: "var(--color-text-secondary)" }}
                          >
                            {workflow.description}
                          </p>
                        )}
                      </div>
                      <ArrowRight size={18} style={{ color: "var(--color-text-muted)" }} />
                    </div>

                    <div className="mt-5 grid gap-3 sm:grid-cols-2">
                      <div
                        className="rounded-xl px-3 py-3"
                        style={{
                          backgroundColor: "rgba(255,255,255,0.03)",
                          border: "1px solid rgba(255,255,255,0.06)",
                        }}
                      >
                        <div className="flex items-center gap-2 text-xs" style={{ color: "var(--color-text-muted)" }}>
                          <CalendarClock size={13} />
                          Trigger
                        </div>
                        <div className="mt-2 text-sm font-medium" style={{ color: "var(--color-text-primary)" }}>
                          {formatWorkflowTriggerLabel(workflow.trigger_type, workflow.trigger_config)}
                        </div>
                        <div className="mt-1 text-xs" style={{ color: "var(--color-text-muted)" }}>
                          {workflow.next_run_at ? formatDateTime(workflow.next_run_at) : "Kein nächster Lauf geplant"}
                        </div>
                      </div>

                      <div
                        className="rounded-xl px-3 py-3"
                        style={{
                          backgroundColor: "rgba(255,255,255,0.03)",
                          border: "1px solid rgba(255,255,255,0.06)",
                        }}
                      >
                        <div className="flex items-center gap-2 text-xs" style={{ color: "var(--color-text-muted)" }}>
                          <Send size={13} />
                          Delivery
                        </div>
                        <div className="mt-2 text-sm font-medium" style={{ color: "var(--color-text-primary)" }}>
                          {deliveryLabel(workflow)}
                        </div>
                        <div className="mt-1 text-xs" style={{ color: "var(--color-text-muted)" }}>
                          Version {workflow.current_version} • {workflow.current_definition.steps.length} Step
                          {workflow.current_definition.steps.length === 1 ? "" : "s"}
                        </div>
                      </div>
                    </div>

                    <div className="mt-4 flex items-center justify-between gap-3 text-xs">
                      <div className="flex items-center gap-3" style={{ color: "var(--color-text-muted)" }}>
                        <span className="inline-flex items-center gap-1.5">
                          <PlayCircle size={13} />
                          {workflow.trigger_type === "scheduled" ? "Automatisch" : "Auf Abruf"}
                        </span>
                        <span>Aktualisiert {timeAgo(workflow.updated_at)}</span>
                      </div>
                      <span className="inline-flex items-center gap-1.5" style={{ color: C.accent }}>
                        Öffnen
                        <ArrowRight size={13} />
                      </span>
                    </div>
                  </GlassCard>
                </Link>
              );
            })}
          </div>
        )}
      </div>
    </AppShell>
  );
}
