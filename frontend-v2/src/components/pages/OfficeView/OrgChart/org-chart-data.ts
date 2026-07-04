/**
 * Static org-chart data for Mission Control.
 *
 * The operator explicitly approved a static source for v1. When the time comes to
 * back this with the live DB, swap `ORG_CHART` for a fetch from
 * `/api/v1/agents` (+ a hierarchy field on the agent table) and keep the
 * `OrgNode` shape. Consumers should not need to change.
 *
 * Notes on hierarchy:
 *   - The operator sits at the root.
 *   - Jarvis branches off as a SEPARATE child of the operator — voice layer,
 *     parallel to Boss, not under him.
 *   - Boss is the central orchestrator. All workers sit under Boss.
 */

import {
  User,                 // Operator
  Mic,                  // Jarvis (voice)
  Crown,                // Boss
  Code2,                // FreeCode
  Zap,                  // Sparky
  ShieldCheck,          // Rex
  Clapperboard,         // Davinci
  PenLine,              // Shakespeare
  Telescope,            // Researcher
  Rocket,               // Deployer
  FlaskConical,         // Tester
  Wrench,               // Installer
  Sparkles,             // Hermes
} from "lucide-react";

import type { OrgChartData, OrgNode } from "./types";

export const ORG_CHART: OrgChartData = {
  nodes: [
    // ── Root ──────────────────────────────────────────────────────────────
    {
      id: "mark",
      name: "Operator",
      role: "Operator",
      runtime: "human",
      status: "online",
      icon: User,
      tagline: "Speaks ideas out loud, the team makes them real.",
      tier: "operator",
      parentId: null,
    },

    // ── Voice branch (parallel to Boss, under the operator) ──────────────
    {
      id: "jarvis",
      name: "Jarvis",
      role: "Voice Assistant",
      runtime: "voice",
      status: "online",
      model: "xai-grok-realtime",
      icon: Mic,
      tagline: "Voice in, answer out — LiveKit + xAI Realtime.",
      tier: "voice",
      parentId: "mark",
    },

    // ── Lead (under Jarvis — visually: operator→Jarvis→Boss vertical line) ──
    // Dispatch-wise Boss reports to the operator; visually we route the line
    // through Jarvis so the voice layer sits "in between" — the operator's
    // mental model is that Jarvis is the channel.
    {
      id: "boss",
      name: "Boss",
      role: "Lead Orchestrator",
      runtime: "host",
      status: "online",
      model: "opus-4.7",
      icon: Crown,
      tagline: "Distributes work. Never implements it himself.",
      tier: "lead",
      parentId: "jarvis",
    },

    // ── Workers (under Boss) ─────────────────────────────────────────────
    {
      id: "freecode",
      name: "FreeCode",
      role: "Developer",
      runtime: "docker",
      status: "online",
      model: "sonnet-4.6",
      icon: Code2,
      tagline: "Frontend, backend, scripts — the all-rounder.",
      tier: "worker",
      parentId: "boss",
    },
    {
      id: "sparky",
      name: "Sparky",
      role: "Developer",
      runtime: "docker",
      status: "online",
      model: "qwen3.6-35b",
      icon: Zap,
      tagline: "Local coding tasks, fast and without overhead.",
      tier: "worker",
      parentId: "boss",
    },
    {
      id: "rex",
      name: "Rex",
      role: "Reviewer",
      runtime: "docker",
      status: "online",
      model: "sonnet-4.6",
      icon: ShieldCheck,
      tagline: "Reviews code, checks security, says stop when needed.",
      tier: "worker",
      parentId: "boss",
    },
    {
      id: "davinci",
      name: "Davinci",
      role: "Content",
      runtime: "docker",
      status: "online",
      model: "qwen3.6-35b",
      icon: Clapperboard,
      tagline: "Images & videos — visual content pipeline.",
      tier: "worker",
      parentId: "boss",
    },
    {
      id: "shakespeare",
      name: "Shakespeare",
      role: "Content",
      runtime: "docker",
      status: "online",
      model: "qwen3.6-35b",
      icon: PenLine,
      tagline: "Blog posts, newsletters, copywriting in DE & EN.",
      tier: "worker",
      parentId: "boss",
    },
    {
      id: "researcher",
      name: "Researcher",
      role: "Research",
      runtime: "docker",
      status: "online",
      model: "qwen3.6-35b",
      icon: Telescope,
      tagline: "Web research, market analysis, distilling data.",
      tier: "worker",
      parentId: "boss",
    },
    {
      id: "deployer",
      name: "Deployer",
      role: "Deploy",
      runtime: "docker",
      status: "online",
      model: "sonnet-4.6",
      icon: Rocket,
      tagline: "Ships code live — Vercel, Docker, CI/CD.",
      tier: "worker",
      parentId: "boss",
    },
    {
      id: "tester",
      name: "Tester",
      role: "QA",
      runtime: "docker",
      status: "online",
      model: "sonnet-4.6",
      icon: FlaskConical,
      tagline: "Tests, E2E, bug reports — before the operator sees them.",
      tier: "worker",
      parentId: "boss",
    },
    {
      id: "installer",
      name: "Installer",
      role: "Plugins",
      runtime: "docker",
      status: "offline",
      model: "sonnet-4.6",
      icon: Wrench,
      tagline: "Installs MCP servers, skills, and plugins.",
      tier: "worker",
      parentId: "boss",
    },
    {
      id: "hermes",
      name: "Hermes",
      role: "Autonomous",
      runtime: "host",
      status: "offline",
      model: "qwen3.6-35b",
      icon: Sparkles,
      tagline: "Morning briefings & scheduled routines.",
      tier: "worker",
      parentId: "boss",
    },
  ],
};

// ── Derived selectors ─────────────────────────────────────────────────────

export function getRoot(data: OrgChartData = ORG_CHART) {
  return data.nodes.find((n) => n.parentId === null) ?? null;
}

export function getChildren(parentId: string | null, data: OrgChartData = ORG_CHART) {
  return data.nodes.filter((n) => n.parentId === parentId);
}

export function getByTier(tier: OrgNode["tier"], data: OrgChartData = ORG_CHART) {
  return data.nodes.filter((n) => n.tier === tier);
}
