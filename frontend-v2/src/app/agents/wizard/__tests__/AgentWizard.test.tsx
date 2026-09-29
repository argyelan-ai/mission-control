import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AgentWizard } from "../AgentWizard";
import { canProceed, initialWizardState, runtimeStepBlockers, type WizardState } from "../types";

vi.mock("@/lib/api", () => ({
  api: {
    agents: { list: vi.fn(async () => []) },
    boards: {},
    runtimes: {
      compatMatrix: vi.fn(async () => ({
        harnesses: [
          { key: "claude", label: "Claude Code" },
          { key: "openclaude", label: "OpenClaude" },
          { key: "omp", label: "omp" },
        ],
        host_harnesses: [
          { key: "claude", label: "Claude Code", protocol: "anthropic", singleton: false, singleton_slug: null, supports_bootstrap: false },
        ],
        runtimes: [
          { slug: "vllm-a", display_name: "vLLM A", protocol: "openai", compatible_harnesses: ["openclaude", "omp"], reasons: {} },
          { slug: "anthropic-claude-cloud", display_name: "Claude Cloud", protocol: "anthropic", compatible_harnesses: ["claude"], reasons: {} },
        ],
      })),
      list: vi.fn(async () => ({ runtimes: [
        { id: "r1", slug: "vllm-a", display_name: "vLLM A", runtime_type: "vllm_docker", model_identifier: "m", enabled: true },
        { id: "an1", slug: "anthropic-claude-cloud", display_name: "Claude Cloud", runtime_type: "cloud", model_identifier: "claude-opus-5", enabled: true },
      ] })),
    },
    cliBridge: { health: vi.fn(async () => ({ reachable: true, bridge_url: "x:18792" })) },
    agentTemplates: { list: vi.fn(async () => []) },
    plugins: { list: vi.fn(async () => ({ plugins: [], total: 0 })) },
    models: { list: vi.fn(async () => ({ models: [] })) },
  },
}));

function wrap(ui: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

describe("AgentWizard shell", () => {
  it("renders all five step labels in the stepper", () => {
    wrap(
      <AgentWizard boards={[]} defaultBoardId={null} onClose={() => {}} onCreated={() => {}} />
    );
    expect(screen.getByText(/Start/)).toBeTruthy();
    expect(screen.getByText(/Identity/)).toBeTruthy();
    expect(screen.getByText(/Runtime/)).toBeTruthy();
    expect(screen.getByText(/Scopes/)).toBeTruthy();
    expect(screen.getByText(/Review/)).toBeTruthy();
  });

  it("Back is disabled on the first step", () => {
    wrap(
      <AgentWizard boards={[]} defaultBoardId={null} onClose={() => {}} onCreated={() => {}} />
    );
    const back = screen.getByRole("button", { name: /Back/ }) as HTMLButtonElement;
    expect(back.disabled).toBe(true);
  });

  it("calls onClose when the close button is clicked", () => {
    const onClose = vi.fn();
    wrap(
      <AgentWizard boards={[]} defaultBoardId={null} onClose={onClose} onCreated={() => {}} />
    );
    fireEvent.click(screen.getByLabelText(/close wizard/i));
    expect(onClose).toHaveBeenCalled();
  });

  it("exposes dialog semantics on the modal container", () => {
    wrap(
      <AgentWizard boards={[]} defaultBoardId={null} onClose={() => {}} onCreated={() => {}} />
    );
    const dialog = screen.getByRole("dialog");
    expect(dialog.getAttribute("aria-modal")).toBe("true");
  });
});

// ── Runtime & Model gate ────────────────────────────────────────────────────
// Regression guard for the "broken agent could still be created" defect
// (PR #626 review, task a1cd29f1): the failure banner made a failed query
// visible but canProceed still let step 2 through for cli-bridge with no
// harness and no runtime binding — exactly how the unprovisionable "Rocket"
// agent came to exist. These tests pin the gate AND its readable refusal;
// removing the gate turns the Next-disabled assertions red.
describe("Runtime & Model gate (canProceed / runtimeStepBlockers)", () => {
  function stateAtStep2(patch: Partial<WizardState>) {
    return { ...initialWizardState(null), step: 2, ...patch };
  }

  it("blocks cli-bridge without a harness — the Rocket damage path", () => {
    const s = stateAtStep2({ agentRuntime: "cli-bridge", harness: null, runtimeId: "" });
    expect(canProceed(s)).toBe(false);
  });

  it("allows cli-bridge with a harness on the fallback runtime", () => {
    const s = stateAtStep2({ agentRuntime: "cli-bridge", harness: "claude", runtimeId: "" });
    expect(canProceed(s)).toBe(true);
    expect(runtimeStepBlockers(s)).toEqual([]);
  });

  it("blocks omp cli-bridge without a runtime binding (restart-loop incident)", () => {
    const s = stateAtStep2({ agentRuntime: "cli-bridge", harness: "omp", runtimeId: "" });
    expect(canProceed(s)).toBe(false);
    expect(runtimeStepBlockers(s).join(" ")).toMatch(/runtime/i);
    expect(canProceed(stateAtStep2({ agentRuntime: "cli-bridge", harness: "omp", runtimeId: "r1" }))).toBe(true);
  });

  it("host needs harness AND runtime (backend 422s a host agent without runtime_id)", () => {
    const neither = stateAtStep2({ agentRuntime: "host", harness: null, runtimeId: "" });
    expect(canProceed(neither)).toBe(false);
    expect(runtimeStepBlockers(neither).join(" ")).toMatch(/harness/i);
    expect(runtimeStepBlockers(neither).join(" ")).toMatch(/runtime/i);
    expect(canProceed(stateAtStep2({ agentRuntime: "host", harness: "claude", runtimeId: "" }))).toBe(false);
    expect(canProceed(stateAtStep2({ agentRuntime: "host", harness: null, runtimeId: "an1" }))).toBe(false);
    expect(canProceed(stateAtStep2({ agentRuntime: "host", harness: "claude", runtimeId: "an1" }))).toBe(true);
  });

  it("manual legitimately needs neither", () => {
    const s = stateAtStep2({ agentRuntime: "manual", harness: null, runtimeId: "" });
    expect(canProceed(s)).toBe(true);
    expect(runtimeStepBlockers(s)).toEqual([]);
  });
});

describe("AgentWizard step 2 — the gate is readable, not a dead button", () => {
  function toRuntimeStep() {
    wrap(
      <AgentWizard boards={[]} defaultBoardId={null} onClose={() => {}} onCreated={() => {}} />
    );
    fireEvent.click(screen.getByRole("button", { name: /Next/ }));
    fireEvent.change(screen.getByPlaceholderText("e.g. Cody"), { target: { value: "Gate" } });
    fireEvent.click(screen.getByRole("button", { name: /Next/ }));
  }

  const nextBtn = () => screen.getByRole("button", { name: /Next/ }) as HTMLButtonElement;

  it("cli-bridge with no harness: Next disabled and the refusal names the missing harness", async () => {
    toRuntimeStep();
    await waitFor(() => screen.getByText("Claude Cloud"));
    expect(nextBtn().disabled).toBe(true);
    const note = screen.getByTestId("step-blockers");
    expect(note.textContent).toMatch(/harness/i);
    expect(note.textContent).toMatch(/provisioned/i);
  });

  it("cli-bridge omp without a runtime: still blocked, refusal names the runtime; picking one unblocks", async () => {
    toRuntimeStep();
    await waitFor(() => screen.getByText("Claude Cloud"));
    fireEvent.click(screen.getByText("omp"));
    await waitFor(() => expect(nextBtn().disabled).toBe(true));
    expect(screen.getByTestId("step-blockers").textContent).toMatch(/runtime/i);
    fireEvent.click(screen.getByText("vLLM A"));
    expect(nextBtn().disabled).toBe(false);
  });

  it("picking a harness unblocks cli-bridge even on the fallback runtime", async () => {
    toRuntimeStep();
    await waitFor(() => screen.getByText("Claude Cloud"));
    fireEvent.click(screen.getByText("Claude Code"));
    expect(nextBtn().disabled).toBe(false);
    expect(screen.queryByTestId("step-blockers")).toBeNull();
  });

  it("host with neither harness nor runtime is blocked with both named", async () => {
    toRuntimeStep();
    await waitFor(() => screen.getByText("Claude Cloud"));
    fireEvent.click(screen.getByText("Host (launchd)"));
    expect(nextBtn().disabled).toBe(true);
    const note = screen.getByTestId("step-blockers").textContent ?? "";
    expect(note).toMatch(/harness/i);
    expect(note).toMatch(/runtime/i);
  });

  it("manual needs neither and advances", async () => {
    toRuntimeStep();
    await waitFor(() => screen.getByText("Claude Cloud"));
    fireEvent.click(screen.getByText("Manual"));
    expect(nextBtn().disabled).toBe(false);
    fireEvent.click(nextBtn());
    // Step 3 reached: the runtime controls are gone and scopes render.
    await waitFor(() => expect(screen.queryByText("Runtime type")).toBeNull());
    expect(screen.getByText(/Scopes \(/)).toBeTruthy();
  });
});
