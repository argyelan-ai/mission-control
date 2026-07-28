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
        // Mirrors backend host_harness_catalog() — the HostHarnessAdapter
        // registry, including `claude` (NOT a singleton) which the wizard's
        // old hardcoded list omitted entirely.
        host_harnesses: [
          { key: "hermes", label: "Hermes", protocol: "openai", singleton: true, singleton_slug: "hermes", supports_bootstrap: true },
          { key: "grok", label: "Grok Build", protocol: "grok", singleton: true, singleton_slug: "grok", supports_bootstrap: true },
          { key: "kimi", label: "Kimi Code", protocol: "kimi", singleton: true, singleton_slug: "kimi", supports_bootstrap: true },
          { key: "claude", label: "Claude Code", protocol: "anthropic", singleton: false, singleton_slug: null, supports_bootstrap: false },
        ],
        runtimes: [
          { slug: "vllm-a", display_name: "vLLM A", protocol: "openai", compatible_harnesses: ["openclaude", "omp"], reasons: { claude: "nur Anthropic" } },
          { slug: "grok-cloud", display_name: "Grok Build (xAI Cloud)", protocol: "grok", compatible_harnesses: [], reasons: {} },
          { slug: "anthropic-claude-cloud", display_name: "Claude Cloud", protocol: "anthropic", compatible_harnesses: ["claude"], reasons: {} },
        ],
      })),
      list: vi.fn(async () => ({ runtimes: [
        { id: "r1", slug: "vllm-a", display_name: "vLLM A", runtime_type: "vllm_docker", model_identifier: "m", enabled: true },
        { id: "gr1", slug: "grok-cloud", display_name: "Grok Build (xAI Cloud)", runtime_type: "grok", model_identifier: "grok-4.5", enabled: true, single_instance: true },
        { id: "an1", slug: "anthropic-claude-cloud", display_name: "Claude Cloud", runtime_type: "cloud", model_identifier: "claude-opus-5", enabled: true },
      ] })),
    },
    cliBridge: { health: vi.fn(async () => ({ reachable: true, bridge_url: "x:18792" })) },
    agents: { list: vi.fn(async () => [] as unknown[]) },
  },
}));

import { api } from "@/lib/api";

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

  it("disables a singleton host harness when an agent with it already exists", async () => {
    // A live Hermes host agent exists → picking hermes again would clobber it
    // (2026-07-12 incident). The picker must disable it, not let the user build
    // a doomed agent that the backend then 422s.
    (api.agents.list as ReturnType<typeof vi.fn>).mockResolvedValueOnce([
      { id: "h1", name: "Hermes", agent_runtime: "host", harness: "hermes" },
    ]);
    const update = vi.fn();
    const state = { ...initialWizardState(null), agentRuntime: "host" as const };
    wrap(<RuntimeStep state={state} update={update} boards={[]} goNext={() => {}} goBack={() => {}} />);
    await waitFor(() => screen.getByText("Grok Build"));
    const hermesBtn = await waitFor(() => {
      const btn = screen.getByText(/Hermes/).closest("button") as HTMLButtonElement;
      if (!btn.disabled) throw new Error("not disabled yet");
      return btn;
    });
    expect(hermesBtn.disabled).toBe(true);
    fireEvent.click(hermesBtn);
    expect(update).not.toHaveBeenCalledWith(expect.objectContaining({ harness: "hermes" }));
  });

  // ── Host harnesses come from the backend registry, not a local list ──────

  it("offers every host harness the backend registry ships, including claude", async () => {
    const update = vi.fn();
    const state = { ...initialWizardState(null), agentRuntime: "host" as const };
    wrap(<RuntimeStep state={state} update={update} boards={[]} goNext={() => {}} goBack={() => {}} />);
    await waitFor(() => screen.getByText("Grok Build"));
    // "claude" was missing from the wizard's hardcoded HOST_HARNESSES list, so
    // a host Claude agent (what Boss is) could not be created at all.
    const claudeBtn = screen.getByText("Claude Code").closest("button") as HTMLButtonElement;
    expect(claudeBtn).toBeTruthy();
    fireEvent.click(claudeBtn);
    expect(update).toHaveBeenCalledWith(expect.objectContaining({ harness: "claude" }));
  });

  it("does NOT disable a non-singleton host harness even when an agent already uses it", async () => {
    // Both a Hermes and a Boss (harness=claude) host agent exist. hermes is a
    // singleton bridge → must grey out. claude has singleton_slug=None, so a
    // second claude host agent is legitimate → must stay pickable. The old
    // blanket "host ⇒ singleton" rule greyed out BOTH.
    (api.agents.list as ReturnType<typeof vi.fn>).mockResolvedValueOnce([
      { id: "h1", name: "Hermes", agent_runtime: "host", harness: "hermes" },
      { id: "b1", name: "Boss", agent_runtime: "host", harness: "claude" },
    ]);
    const update = vi.fn();
    const state = { ...initialWizardState(null), agentRuntime: "host" as const };
    wrap(<RuntimeStep state={state} update={update} boards={[]} goNext={() => {}} goBack={() => {}} />);
    // Waiting for hermes to go disabled proves the agent list has RESOLVED —
    // without this the claude assertion below would pass vacuously on the
    // first render, before the singleton check has any data.
    // (regex, not exact: a taken harness renders "Hermes ✓")
    await waitFor(() => {
      const hermes = screen.getByText(/Hermes/).closest("button") as HTMLButtonElement;
      if (!hermes.disabled) throw new Error("agent list not applied yet");
    });
    const claudeBtn = screen.getByText("Claude Code").closest("button") as HTMLButtonElement;
    expect(claudeBtn.disabled).toBe(false);
    fireEvent.click(claudeBtn);
    expect(update).toHaveBeenCalledWith(expect.objectContaining({ harness: "claude" }));
  });

  it("filters providers by the protocol the registry ships with the host harness", async () => {
    // No local protocol map any more: claude → anthropic comes from
    // host_harnesses, so the anthropic runtime is selectable and the
    // openai/grok ones are not.
    const state = { ...initialWizardState(null), agentRuntime: "host" as const, harness: "claude" as const };
    wrap(<RuntimeStep state={state} update={() => {}} boards={[]} goNext={() => {}} goBack={() => {}} />);
    await waitFor(() => screen.getByText("Claude Cloud"));
    const anthropic = screen.getByText("Claude Cloud").closest("button") as HTMLButtonElement;
    expect(anthropic.disabled).toBe(false);
    const vllm = screen.getByText("vLLM A").closest("button") as HTMLButtonElement;
    expect(vllm.disabled).toBe(true);
    const grok = screen.getByText("Grok Build (xAI Cloud)").closest("button") as HTMLButtonElement;
    expect(grok.disabled).toBe(true);
  });
});
