/**
 * Org-Chart Types — describes a single node in the Mission Control hierarchy.
 *
 * Keep this file dependency-free so a future DB-backed swap (e.g. fetching
 * `/api/v1/agents` + a hierarchy field) drops in without touching consumers.
 */

import type { LucideIcon } from "lucide-react";

export type OrgRuntime = "human" | "voice" | "host" | "docker";

// Message keys in the office.* namespace — t() at the render site.
export type OrgRoleKey =
  | "roleOperator"
  | "roleVoiceAssistant"
  | "roleLeadOrchestrator"
  | "roleDeveloper"
  | "roleReviewer"
  | "roleContent"
  | "roleResearch"
  | "roleDeploy"
  | "roleQA"
  | "rolePlugins"
  | "roleAutonomous";

export type OrgStatus = "online" | "working" | "offline" | "warning" | "error";

export type OrgTier = "operator" | "voice" | "lead" | "worker";

export interface OrgNode {
  /** Stable identifier — match `agent.slug` when DB-backed. */
  id: string;
  /** Display name. */
  name: string;
  /** Message key (office.*) for the role label shown on the card. */
  roleKey: OrgRoleKey;
  /** Runtime environment the agent lives in. */
  runtime: OrgRuntime;
  /** Currently presumed status (static for v1; live data later). */
  status: OrgStatus;
  /** Short model identifier — e.g. `opus-4.7`, `sonnet-4.6`. Empty for non-LLM. */
  model?: string;
  /** Lucide icon component that visually communicates the role. */
  icon: LucideIcon;
  /** Message key (office.*) for the one-sentence tagline on the card. */
  taglineKey: string;
  /** Visual tier — drives card variant (sizing, glow, position). */
  tier: OrgTier;
  /** Parent id — null for the root (the operator). */
  parentId: string | null;
}

export interface OrgChartData {
  /** Flat list — hierarchy expressed via `parentId`. Root has parentId=null. */
  nodes: OrgNode[];
}
