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
      roleKey: "roleOperator",
      runtime: "human",
      status: "online",
      icon: User,
      taglineKey: "taglineMark",
      tier: "operator",
      parentId: null,
    },

    // ── Voice branch (parallel to Boss, under the operator) ──────────────
    {
      id: "jarvis",
      name: "Jarvis",
      roleKey: "roleVoiceAssistant",
      runtime: "voice",
      status: "online",
      model: "xai-grok-realtime",
      icon: Mic,
      taglineKey: "taglineJarvis",
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
      roleKey: "roleLeadOrchestrator",
      runtime: "host",
      status: "online",
      model: "opus-4.7",
      icon: Crown,
      taglineKey: "taglineBoss",
      tier: "lead",
      parentId: "jarvis",
    },

    // ── Workers (under Boss) ─────────────────────────────────────────────
    {
      id: "freecode",
      name: "FreeCode",
      roleKey: "roleDeveloper",
      runtime: "docker",
      status: "online",
      model: "sonnet-4.6",
      icon: Code2,
      taglineKey: "taglineFreecode",
      tier: "worker",
      parentId: "boss",
    },
    {
      id: "sparky",
      name: "Sparky",
      roleKey: "roleDeveloper",
      runtime: "docker",
      status: "online",
      model: "qwen3.6-35b",
      icon: Zap,
      taglineKey: "taglineSparky",
      tier: "worker",
      parentId: "boss",
    },
    {
      id: "rex",
      name: "Rex",
      roleKey: "roleReviewer",
      runtime: "docker",
      status: "online",
      model: "sonnet-4.6",
      icon: ShieldCheck,
      taglineKey: "taglineRex",
      tier: "worker",
      parentId: "boss",
    },
    {
      id: "davinci",
      name: "Davinci",
      roleKey: "roleContent",
      runtime: "docker",
      status: "online",
      model: "qwen3.6-35b",
      icon: Clapperboard,
      taglineKey: "taglineDavinci",
      tier: "worker",
      parentId: "boss",
    },
    {
      id: "shakespeare",
      name: "Shakespeare",
      roleKey: "roleContent",
      runtime: "docker",
      status: "online",
      model: "qwen3.6-35b",
      icon: PenLine,
      taglineKey: "taglineShakespeare",
      tier: "worker",
      parentId: "boss",
    },
    {
      id: "researcher",
      name: "Researcher",
      roleKey: "roleResearch",
      runtime: "docker",
      status: "online",
      model: "qwen3.6-35b",
      icon: Telescope,
      taglineKey: "taglineResearcher",
      tier: "worker",
      parentId: "boss",
    },
    {
      id: "deployer",
      name: "Deployer",
      roleKey: "roleDeploy",
      runtime: "docker",
      status: "online",
      model: "sonnet-4.6",
      icon: Rocket,
      taglineKey: "taglineDeployer",
      tier: "worker",
      parentId: "boss",
    },
    {
      id: "tester",
      name: "Tester",
      roleKey: "roleQA",
      runtime: "docker",
      status: "online",
      model: "sonnet-4.6",
      icon: FlaskConical,
      taglineKey: "taglineTester",
      tier: "worker",
      parentId: "boss",
    },
    {
      id: "installer",
      name: "Installer",
      roleKey: "rolePlugins",
      runtime: "docker",
      status: "offline",
      model: "sonnet-4.6",
      icon: Wrench,
      taglineKey: "taglineInstaller",
      tier: "worker",
      parentId: "boss",
    },
    {
      id: "hermes",
      name: "Hermes",
      roleKey: "roleAutonomous",
      runtime: "host",
      status: "offline",
      model: "qwen3.6-35b",
      icon: Sparkles,
      taglineKey: "taglineHermes",
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
