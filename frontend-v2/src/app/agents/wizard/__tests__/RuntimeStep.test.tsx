import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RuntimeStep } from "../steps/RuntimeStep";
import { initialWizardState } from "../types";

vi.mock("@/lib/api", () => ({
  api: {
    runtimes: {
      compatMatrix: vi.fn(async () => ({
        harnesses: [
          { key: "claude", label: "Claude Code" },
          { key: "openclaude", label: "OpenClaude" },
          { key: "omp", label: "omp" },
        ],
        runtimes: [
          { slug: "vllm-a", display_name: "vLLM A", protocol: "openai", compatible_harnesses: ["openclaude", "omp"], reasons: { claude: "nur Anthropic" } },
          { slug: "grok-cloud", display_name: "Grok Build (xAI Cloud)", protocol: "grok", compatible_harnesses: [], reasons: {} },
        ],
      })),
      list: vi.fn(async () => ({ runtimes: [
        { id: "r1", slug: "vllm-a", display_name: "vLLM A", runtime_type: "vllm_docker", model_identifier: "m", enabled: true },
        { id: "gr1", slug: "grok-cloud", display_name: "Grok Build (xAI Cloud)", runtime_type: "grok", model_identifier: "grok-4.5", enabled: true, single_instance: true },
      ] })),
    },
    cliBridge: { health: vi.fn(async () => ({ reachable: true, bridge_url: "x:18792" })) },
  },
}));

function wrap(ui: React.ReactNode) {
  const qc = new QueryClient();
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

describe("RuntimeStep", () => {
  it("picking a harness updates state", async () => {
    const update = vi.fn();
    wrap(<RuntimeStep state={initialWizardState(null)} update={update} boards={[]} goNext={() => {}} goBack={() => {}} />);
    await waitFor(() => screen.getByText("OpenClaude"));
    fireEvent.click(screen.getByText("OpenClaude"));
    expect(update).toHaveBeenCalledWith(expect.objectContaining({ harness: "openclaude" }));
  });

  it("disables a provider incompatible with the chosen harness", async () => {
    const state = { ...initialWizardState(null), harness: "claude" as const };
    wrap(<RuntimeStep state={state} update={() => {}} boards={[]} goNext={() => {}} goBack={() => {}} />);
    await waitFor(() => screen.getByText("vLLM A"));
    const opt = screen.getByText("vLLM A").closest("button") as HTMLButtonElement;
    expect(opt.disabled).toBe(true);
  });

  it("clears the orphaned model when switching to an incompatible harness clears the runtime", async () => {
    const update = vi.fn();
    const state = { ...initialWizardState(null), harness: "openclaude" as const, runtimeId: "r1", model: "m" };
    wrap(<RuntimeStep state={state} update={update} boards={[]} goNext={() => {}} goBack={() => {}} />);
    await waitFor(() => screen.getByText("Claude Code"));
    fireEvent.click(screen.getByText("Claude Code"));
    expect(update).toHaveBeenCalledWith(
      expect.objectContaining({ harness: "claude", runtimeId: "", model: "" })
    );
  });

  // ── Host harnesses: grok (ADR-066) ──────────────────────────────────────────

  it("host runtime offers the grok harness (not the cli-bridge matrix list)", async () => {
    const update = vi.fn();
    const state = { ...initialWizardState(null), agentRuntime: "host" as const };
    wrap(<RuntimeStep state={state} update={update} boards={[]} goNext={() => {}} goBack={() => {}} />);
    await waitFor(() => screen.getByText("Grok Build"));
    expect(screen.getByText("Hermes")).toBeTruthy();
    // cli-bridge-only harnesses must NOT appear for host.
    expect(screen.queryByText("OpenClaude")).toBeNull();
    fireEvent.click(screen.getByText("Grok Build"));
    expect(update).toHaveBeenCalledWith(expect.objectContaining({ harness: "grok" }));
  });

  it("host+grok: only the grok-cloud runtime is compatible; openai providers disabled", async () => {
    const update = vi.fn();
    const state = { ...initialWizardState(null), agentRuntime: "host" as const, harness: "grok" as const };
    wrap(<RuntimeStep state={state} update={update} boards={[]} goNext={() => {}} goBack={() => {}} />);
    await waitFor(() => screen.getByText("Grok Build (xAI Cloud)"));
    const grokRt = screen.getByText("Grok Build (xAI Cloud)").closest("button") as HTMLButtonElement;
    // single-instance grok-cloud stays selectable for a host agent.
    expect(grokRt.disabled).toBe(false);
    const vllm = screen.getByText("vLLM A").closest("button") as HTMLButtonElement;
    expect(vllm.disabled).toBe(true); // openai protocol, incompatible with grok
    fireEvent.click(grokRt);
    expect(update).toHaveBeenCalledWith(expect.objectContaining({ runtimeId: "gr1" }));
  });
});
