import type { Board, Harness, HostHarness } from "@/lib/types";

export type StartMode = "custom" | "template" | "duplicate";
export type WizardAgentRuntime = "cli-bridge" | "host" | "manual";

export interface WizardState {
  step: number; // 0..4
  startMode: StartMode;
  templateId: string | null;
  sourceAgentId: string | null;
  // Identity
  name: string;
  emoji: string;
  role: string;
  boardId: string;
  isBoardLead: boolean;
  soulMd: string | null; // template/duplicate persona override
  // Runtime & model
  agentRuntime: WizardAgentRuntime;
  // cli-bridge harness OR a host-only harness (hermes/grok) for the host runtime.
  harness: Harness | HostHarness | null;
  runtimeId: string; // LLM runtime slug/uuid; "" = fallback
  model: string;
  // Rights & skills
  scopes: string[];
  skillFilter: string[] | null;
  cliPlugins: string[] | null;
  // Provisioning result
  createdAgentId: string | null;
  createdToken: string | null;
}

export interface WizardStepProps {
  state: WizardState;
  update: (patch: Partial<WizardState>) => void;
  boards: Board[];
  goNext: () => void;
  goBack: () => void;
}

export const WIZARD_STEPS: { key: string; label: string }[] = [
  { key: "start", label: "Start" },
  { key: "identity", label: "Identity" },
  { key: "runtime", label: "Runtime & Model" },
  { key: "rights", label: "Scopes & Skills" },
  { key: "review", label: "Review & Provision" },
];

export function initialWizardState(defaultBoardId: string | null): WizardState {
  return {
    step: 0,
    startMode: "custom",
    templateId: null,
    sourceAgentId: null,
    name: "",
    emoji: "",
    role: "",
    boardId: defaultBoardId ?? "",
    isBoardLead: false,
    soulMd: null,
    agentRuntime: "cli-bridge",
    harness: null,
    runtimeId: "",
    model: "",
    scopes: [],
    skillFilter: null,
    cliPlugins: null,
    createdAgentId: null,
    createdToken: null,
  };
}

// Per-step gate. Step 4 (review) has no "next" — it provisions.
export function canProceed(state: WizardState): boolean {
  switch (state.step) {
    case 0: // start
      if (state.startMode === "template") return !!state.templateId;
      if (state.startMode === "duplicate") return !!state.sourceAgentId;
      return true; // custom
    case 1: // identity — name is the only required field
      return state.name.trim().length > 0;
    case 2: // runtime & model — the per-runtime rules live in
      // runtimeStepBlockers() so the footer can name WHAT is missing.
      return runtimeStepBlockers(state).length === 0;
    case 3: // rights — always has a concrete scope list (never empty = all)
      return state.scopes.length > 0;
    default:
      return false;
  }
}

// WHY the Runtime & Model step cannot advance, in operator-readable form.
// Empty array = the step is complete; canProceed() case 2 is exactly this.
//
// - cli-bridge: a harness is mandatory. With harness NULL the backend derives
//   it from the bound runtime's protocol (derive_harness) — but the fallback
//   ("docker-compose env") has no runtime row, so a harness-less fallback
//   agent gets effective harness NULL and provisions into nothing (the
//   "Rocket" incident: created, permanently stuck, deleted). omp additionally
//   cannot boot without a runtime binding at all
//   (HARNESSES_REQUIRING_RUNTIME_BINDING, incident 2026-09-05:
//   OPENAI_BASE_URL/OPENAI_MODEL come only from the bound runtime row →
//   container restart loop). Other harnesses survive the fallback env, so a
//   non-omp cli-bridge agent may legitimately run on the fallback.
// - host: the backend 422s a host agent without runtime_id
//   (routers/agents.py _host_requires_runtime_id), and host-only harnesses
//   like grok are NOT derivable (derive_harness returns None for a
//   grok-cloud runtime), so the wizard must carry the explicit harness too —
//   both rows are rendered for host, both are required.
// - manual: no auto-provisioning — legitimately created with neither.
export function runtimeStepBlockers(state: WizardState): string[] {
  if (state.agentRuntime === "manual") return [];
  const blockers: string[] = [];
  if (!state.harness) {
    blockers.push("Pick a harness — an agent without one cannot be provisioned afterwards.");
  }
  if (state.agentRuntime === "host" && !state.runtimeId) {
    blockers.push("Pick an LLM runtime — host agents need a runtime binding at creation time.");
  }
  if (state.agentRuntime === "cli-bridge" && state.harness === "omp" && !state.runtimeId) {
    blockers.push("Pick an LLM runtime — the omp harness cannot boot without a runtime binding.");
  }
  return blockers;
}
