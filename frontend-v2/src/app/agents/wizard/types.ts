import type { Board, Harness } from "@/lib/types";

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
  harness: Harness | null;
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
  { key: "identity", label: "Identität" },
  { key: "runtime", label: "Runtime & Modell" },
  { key: "rights", label: "Rechte & Skills" },
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
    case 2: // runtime & model — host needs an LLM runtime binding
      if (state.agentRuntime === "host") return !!state.runtimeId;
      return true;
    case 3: // rights — always has a concrete scope list (never empty = all)
      return state.scopes.length > 0;
    default:
      return false;
  }
}
