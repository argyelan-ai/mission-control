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
        ],
      })),
      list: vi.fn(async () => ({ runtimes: [{ id: "r1", slug: "vllm-a", display_name: "vLLM A", runtime_type: "vllm_docker", model_identifier: "m", enabled: true }] })),
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
});
