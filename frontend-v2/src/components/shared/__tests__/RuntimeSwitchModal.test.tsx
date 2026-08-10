/**
 * RuntimeSwitchModal — Phase 15 T3.6 vitest.
 *
 * Coverage:
 *   1. preview is fetched and runtimes render side-by-side
 *   2. image_switched=true shows the orange "Container-Image wird gewechselt" banner
 *   3. agent.current_task_id present → busy banner + force toggle visible
 *   4. submit calls onConfirm with force_when_in_progress and closes on success
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RuntimeSwitchModal } from "../RuntimeSwitchModal";
import { api } from "@/lib/api";
import type { Agent, CompatMatrix, RuntimeSwitchPreview } from "@/lib/types";

const mkCompatMatrix = (overrides: Partial<CompatMatrix> = {}): CompatMatrix => ({
  harnesses: [
    { key: "claude", label: "Claude Code" },
    { key: "openclaude", label: "OpenClaude" },
    { key: "omp", label: "omp" },
  ],
  // Host-harness registry (backend host_harness_catalog). The switch modal
  // does not render it — the agent wizard does — but the response shape is
  // shared, so the fixture carries it.
  host_harnesses: [
    { key: "hermes", label: "Hermes", protocol: "openai", singleton: true, singleton_slug: "hermes", supports_bootstrap: true },
    { key: "claude", label: "Claude Code", protocol: "anthropic", singleton: false, singleton_slug: null, supports_bootstrap: false },
  ],
  runtimes: [
    {
      slug: "qwen-general",
      display_name: "Qwen 3.6",
      protocol: "openai",
      compatible_harnesses: ["openclaude", "omp"],
      reasons: { claude: "Claude Code unterstützt nur Anthropic-Protokoll-Runtimes" },
    },
  ],
  ...overrides,
});

function renderWithQuery(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

const mkAgent = (overrides: Partial<Agent> = {}): Agent =>
  ({
    id: "agent-1",
    name: "Davinci",
    agent_runtime: "cli-bridge",
    runtime_id: "rt-old",
    status: "online",
    is_board_lead: false,
    context_tokens: 0,
    context_max: 200_000,
    // Backend-derived switch eligibility (Agent.runtime_switchable). The modal
    // reads this instead of re-deriving `harness === "hermes"`, so every
    // fixture has to state what the API would have said.
    runtime_switchable: true,
    runtime_switch_blocked_reason: null,
    ...overrides,
  }) as Agent;

const mkPreview = (overrides: Partial<RuntimeSwitchPreview> = {}): RuntimeSwitchPreview => ({
  old_runtime: {
    id: "rt-old",
    slug: "anthropic-claude-sonnet",
    display_name: "Claude Sonnet",
    runtime_type: "cloud",
    model_identifier: "claude-sonnet-4-6",
  },
  new_runtime: {
    id: "rt-new",
    slug: "qwen-general",
    display_name: "Qwen 3.6",
    runtime_type: "vllm_docker",
    model_identifier: "Qwen/Qwen3.6-35B",
  },
  image_switched: false,
  duration_ms: 0,
  warnings: [],
  dry_run: true,
  health: null,
  ...overrides,
});

describe("RuntimeSwitchModal", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("renders runtime pair from dry-run preview", async () => {
    vi.spyOn(api.agents, "previewRuntimeSwitch").mockResolvedValue(mkPreview());
    const agent = mkAgent();
    renderWithQuery(
      <RuntimeSwitchModal
        open
        onClose={() => {}}
        agent={agent}
        targetRuntimeId="rt-new"
        onConfirm={async () => null}
      />,
    );
    await waitFor(() => expect(screen.getByText("Claude Sonnet")).toBeInTheDocument());
    expect(screen.getByText("Qwen 3.6")).toBeInTheDocument();
  });

  it("shows the image-rebuild banner when image_switched=true", async () => {
    vi.spyOn(api.agents, "previewRuntimeSwitch").mockResolvedValue(
      mkPreview({ image_switched: true }),
    );
    renderWithQuery(
      <RuntimeSwitchModal
        open
        onClose={() => {}}
        agent={mkAgent()}
        targetRuntimeId="rt-new"
        onConfirm={async () => null}
      />,
    );
    await waitFor(() => expect(screen.getByText(/container image will change/i)).toBeInTheDocument());
  });

  it("shows in-progress force toggle when agent has current_task_id", async () => {
    vi.spyOn(api.agents, "previewRuntimeSwitch").mockResolvedValue(mkPreview());
    const agent = mkAgent({ current_task_id: "task-1234" });
    renderWithQuery(
      <RuntimeSwitchModal
        open
        onClose={() => {}}
        agent={agent}
        targetRuntimeId="rt-new"
        onConfirm={async () => null}
      />,
    );
    await waitFor(() => expect(screen.getByText(/agent is working on an active task/i)).toBeInTheDocument());
    expect(screen.getByLabelText(/switch anyway/i)).toBeInTheDocument();
  });

  it("submit calls onConfirm with force flag value and shows the success state (no auto-close)", async () => {
    vi.spyOn(api.agents, "previewRuntimeSwitch").mockResolvedValue(mkPreview());
    vi.spyOn(api.agents, "runtimeSwitchProgress").mockResolvedValue({ step: "done" });
    const onConfirm = vi.fn().mockResolvedValue(mkPreview());
    const onClose = vi.fn();
    renderWithQuery(
      <RuntimeSwitchModal
        open
        onClose={onClose}
        agent={mkAgent()}
        targetRuntimeId="rt-new"
        onConfirm={onConfirm}
      />,
    );
    await waitFor(() => expect(screen.getByText("Qwen 3.6")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: /switch/i }));
    await waitFor(() => expect(onConfirm).toHaveBeenCalledWith({ force_when_in_progress: false }));
    await waitFor(() => expect(screen.getByText(/switch complete/i)).toBeInTheDocument());
    expect(onClose).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: /close/i }));
    expect(onClose).toHaveBeenCalled();
  });

  // Task #26 — the switch now auto-triggers the agent restart; the success
  // panel must say plainly whether that restart ran, was skipped (busy
  // agent / opt-out), or was attempted and failed.
  it("shows a restart-pending note when the switch result reports restart_skipped", async () => {
    vi.spyOn(api.agents, "previewRuntimeSwitch").mockResolvedValue(mkPreview());
    vi.spyOn(api.agents, "runtimeSwitchProgress").mockResolvedValue({ step: "restart_skipped" });
    const onConfirm = vi.fn().mockResolvedValue(
      mkPreview({
        dry_run: false,
        restart_skipped: true,
        restart_skip_reason: "Task abc123 läuft noch — Neustart übersprungen.",
      }),
    );
    renderWithQuery(
      <RuntimeSwitchModal
        open
        onClose={() => {}}
        agent={mkAgent()}
        targetRuntimeId="rt-new"
        onConfirm={onConfirm}
      />,
    );
    await waitFor(() => expect(screen.getByText("Qwen 3.6")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: /switch/i }));
    await waitFor(() => expect(screen.getByTestId("restart-skipped-note")).toBeInTheDocument());
    expect(screen.getByText(/Neustart übersprungen/)).toBeInTheDocument();
    expect(screen.queryByTestId("restart-done-note")).not.toBeInTheDocument();
  });

  it("shows a restart-failed note when the switch result reports restart_failed", async () => {
    vi.spyOn(api.agents, "previewRuntimeSwitch").mockResolvedValue(mkPreview());
    vi.spyOn(api.agents, "runtimeSwitchProgress").mockResolvedValue({ step: "restart_failed" });
    const onConfirm = vi.fn().mockResolvedValue(
      mkPreview({ dry_run: false, restart_failed: true }),
    );
    renderWithQuery(
      <RuntimeSwitchModal
        open
        onClose={() => {}}
        agent={mkAgent()}
        targetRuntimeId="rt-new"
        onConfirm={onConfirm}
      />,
    );
    await waitFor(() => expect(screen.getByText("Qwen 3.6")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: /switch/i }));
    await waitFor(() => expect(screen.getByTestId("restart-failed-note")).toBeInTheDocument());
    expect(screen.getByText(/Retry the restart manually/)).toBeInTheDocument();
  });

  it("shows the auto-restarted confirmation when neither skipped nor failed", async () => {
    vi.spyOn(api.agents, "previewRuntimeSwitch").mockResolvedValue(mkPreview());
    vi.spyOn(api.agents, "runtimeSwitchProgress").mockResolvedValue({ step: "done" });
    const onConfirm = vi.fn().mockResolvedValue(mkPreview({ dry_run: false }));
    renderWithQuery(
      <RuntimeSwitchModal
        open
        onClose={() => {}}
        agent={mkAgent()}
        targetRuntimeId="rt-new"
        onConfirm={onConfirm}
      />,
    );
    await waitFor(() => expect(screen.getByText("Qwen 3.6")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: /switch/i }));
    await waitFor(() => expect(screen.getByTestId("restart-done-note")).toBeInTheDocument());
    expect(screen.getByText(/Agent restarted automatically/)).toBeInTheDocument();
  });

  it("polls runtimeSwitchProgress while submitting and highlights the active step", async () => {
    vi.spyOn(api.agents, "previewRuntimeSwitch").mockResolvedValue(mkPreview());
    vi.spyOn(api.agents, "runtimeSwitchProgress").mockResolvedValue({ step: "restarting" });
    let resolveConfirm: (v: RuntimeSwitchPreview | null) => void = () => {};
    const onConfirm = vi.fn(
      () => new Promise<RuntimeSwitchPreview | null>((resolve) => (resolveConfirm = resolve)),
    );
    renderWithQuery(
      <RuntimeSwitchModal
        open
        onClose={() => {}}
        agent={mkAgent()}
        targetRuntimeId="rt-new"
        onConfirm={onConfirm}
      />,
    );
    await waitFor(() => expect(screen.getByText("Qwen 3.6")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: /switch/i }));
    await waitFor(() => expect(screen.getByText("Restarting container")).toBeInTheDocument());
    resolveConfirm(null);
  });

  it("shows a red rolled_back banner with the error when the switch fails and rolls back", async () => {
    vi.spyOn(api.agents, "previewRuntimeSwitch").mockResolvedValue(mkPreview());
    vi.spyOn(api.agents, "runtimeSwitchProgress").mockResolvedValue({
      step: "rolled_back",
      error: "health check failed",
    });
    let rejectConfirm: (e: Error) => void = () => {};
    const onConfirm = vi.fn(
      () => new Promise<RuntimeSwitchPreview | null>((_resolve, reject) => (rejectConfirm = reject)),
    );
    renderWithQuery(
      <RuntimeSwitchModal
        open
        onClose={() => {}}
        agent={mkAgent()}
        targetRuntimeId="rt-new"
        onConfirm={onConfirm}
      />,
    );
    await waitFor(() => expect(screen.getByText("Qwen 3.6")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: /switch/i }));
    await waitFor(() => expect(screen.getByText(/health check failed/i)).toBeInTheDocument());
    expect(screen.getByTestId("rolled-back-banner")).toBeInTheDocument();
    rejectConfirm(new Error("health check failed"));
  });

  it("resets the completed success state when the modal is closed and reopened", async () => {
    vi.spyOn(api.agents, "previewRuntimeSwitch").mockResolvedValue(mkPreview());
    vi.spyOn(api.agents, "runtimeSwitchProgress").mockResolvedValue({ step: "done" });
    const onConfirm = vi.fn().mockResolvedValue(mkPreview());
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { rerender } = render(
      <QueryClientProvider client={qc}>
        <RuntimeSwitchModal
          open
          onClose={() => {}}
          agent={mkAgent()}
          targetRuntimeId="rt-new"
          onConfirm={onConfirm}
        />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(screen.getByText("Qwen 3.6")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: /switch/i }));
    await waitFor(() => expect(screen.getByText(/switch complete/i)).toBeInTheDocument());

    rerender(
      <QueryClientProvider client={qc}>
        <RuntimeSwitchModal
          open={false}
          onClose={() => {}}
          agent={mkAgent()}
          targetRuntimeId="rt-new"
          onConfirm={onConfirm}
        />
      </QueryClientProvider>,
    );
    rerender(
      <QueryClientProvider client={qc}>
        <RuntimeSwitchModal
          open
          onClose={() => {}}
          agent={mkAgent()}
          targetRuntimeId="rt-new"
          onConfirm={onConfirm}
        />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(screen.getByText("Qwen 3.6")).toBeInTheDocument());
    expect(screen.queryByText(/switch complete/i)).not.toBeInTheDocument();
  });

  it("shows verbatim D-10 lock banner when target.single_instance is true", async () => {
    vi.spyOn(api.agents, "previewRuntimeSwitch").mockResolvedValue(
      mkPreview({
        new_runtime: {
          id: "rt-hermes",
          slug: "hermes-host",
          display_name: "Hermes",
          runtime_type: "hermes",
          model_identifier: null,
          single_instance: true,
        },
      }),
    );
    renderWithQuery(
      <RuntimeSwitchModal
        open
        onClose={() => {}}
        agent={mkAgent()}
        targetRuntimeId="rt-hermes"
        onConfirm={async () => null}
      />,
    );
    await waitFor(() =>
      expect(
        screen.getByText("Single-instance runtime — switch not possible"),
      ).toBeInTheDocument(),
    );
    expect(screen.getByTestId("single-instance-lock-banner")).toBeInTheDocument();
  });

  it("disables submit when target runtime is single_instance", async () => {
    vi.spyOn(api.agents, "previewRuntimeSwitch").mockResolvedValue(
      mkPreview({
        new_runtime: {
          id: "rt-hermes",
          slug: "hermes-host",
          display_name: "Hermes",
          runtime_type: "hermes",
          model_identifier: null,
          single_instance: true,
        },
      }),
    );
    const onConfirm = vi.fn().mockResolvedValue(null);
    renderWithQuery(
      <RuntimeSwitchModal
        open
        onClose={() => {}}
        agent={mkAgent()}
        targetRuntimeId="rt-hermes"
        onConfirm={onConfirm}
      />,
    );
    await waitFor(() => expect(screen.getByText("Hermes")).toBeInTheDocument());
    const submit = screen.getByRole("button", { name: /switch/i });
    expect(submit).toBeDisabled();
    // Click is a no-op because button is disabled — onConfirm must NOT fire
    await userEvent.click(submit);
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("disables submit when current (source) runtime is single_instance", async () => {
    vi.spyOn(api.agents, "previewRuntimeSwitch").mockResolvedValue(
      mkPreview({
        old_runtime: {
          id: "rt-hermes",
          slug: "hermes-host",
          display_name: "Hermes",
          runtime_type: "hermes",
          model_identifier: null,
          single_instance: true,
        },
      }),
    );
    renderWithQuery(
      <RuntimeSwitchModal
        open
        onClose={() => {}}
        agent={mkAgent()}
        targetRuntimeId="rt-new"
        onConfirm={async () => null}
      />,
    );
    await waitFor(() =>
      expect(
        screen.getByText("Single-instance runtime — switch not possible"),
      ).toBeInTheDocument(),
    );
    expect(screen.getByRole("button", { name: /switch/i })).toBeDisabled();
  });

  it("regression: previewRuntimeSwitch is called with staleTime: 0 (fresh probe per open)", async () => {
    // The modal uses queryKey [..., agent.id, targetRuntimeId] with staleTime: 0
    // so each open re-fetches. Assert the fetcher fires when the modal opens.
    const spy = vi.spyOn(api.agents, "previewRuntimeSwitch").mockResolvedValue(mkPreview());
    renderWithQuery(
      <RuntimeSwitchModal
        open
        onClose={() => {}}
        agent={mkAgent()}
        targetRuntimeId="rt-new"
        onConfirm={async () => null}
      />,
    );
    await waitFor(() => expect(spy).toHaveBeenCalledTimes(1));
    expect(spy).toHaveBeenCalledWith(
      "agent-1",
      expect.objectContaining({ runtime_id: "rt-new" }),
    );
  });

  describe("host in-place switch (ADR-060)", () => {
    // A host agent that owns a HostHarnessAdapter switches in place — the
    // backend's `_is_host_inplace()` bypasses the single_instance hard-block
    // for it (see agent_runtime_switch.py). The frontend must mirror that:
    // enable the picker/submit and show an in-place hint instead of the D-10
    // lock banner, even though the agent's *current* runtime row (the Hermes
    // runtime itself) is single_instance=true.
    //
    // Which host harnesses qualify is the BACKEND's answer
    // (`agent.runtime_switchable`). The modal used to hardcode
    // `harness === "hermes"`, which locked grok/kimi/claude host agents out of
    // the picker after their adapters were registered — the tests below pin
    // that the harness string is no longer part of the decision.
    it("enables the runtime picker for host+hermes agents and shows the in-place hint", async () => {
      vi.spyOn(api.agents, "previewRuntimeSwitch").mockResolvedValue(
        mkPreview({
          old_runtime: {
            id: "rt-hermes",
            slug: "hermes-host",
            display_name: "Hermes",
            runtime_type: "hermes",
            model_identifier: null,
            single_instance: true,
          },
        }),
      );
      const agent = mkAgent({ agent_runtime: "host", harness: "hermes" as Agent["harness"] });
      renderWithQuery(
        <RuntimeSwitchModal
          open
          onClose={() => {}}
          agent={agent}
          targetRuntimeId="rt-new"
          onConfirm={async () => null}
        />,
      );
      await waitFor(() => expect(screen.getByText("Qwen 3.6")).toBeInTheDocument());
      expect(screen.getByRole("button", { name: /switch/i })).toBeEnabled();
      expect(screen.getByText(/in-place/i)).toBeInTheDocument();
      expect(screen.queryByTestId("single-instance-lock-banner")).not.toBeInTheDocument();
    });

    it("submit still calls onConfirm for a host+hermes in-place switch", async () => {
      vi.spyOn(api.agents, "previewRuntimeSwitch").mockResolvedValue(
        mkPreview({
          old_runtime: {
            id: "rt-hermes",
            slug: "hermes-host",
            display_name: "Hermes",
            runtime_type: "hermes",
            model_identifier: null,
            single_instance: true,
          },
        }),
      );
      const agent = mkAgent({ agent_runtime: "host", harness: "hermes" as Agent["harness"] });
      const onConfirm = vi.fn().mockResolvedValue(mkPreview());
      renderWithQuery(
        <RuntimeSwitchModal
          open
          onClose={() => {}}
          agent={agent}
          targetRuntimeId="rt-new"
          onConfirm={onConfirm}
        />,
      );
      await waitFor(() => expect(screen.getByText("Qwen 3.6")).toBeInTheDocument());
      await userEvent.click(screen.getByRole("button", { name: /switch/i }));
      await waitFor(() => expect(onConfirm).toHaveBeenCalledWith({ force_when_in_progress: false }));
    });

    // The regression that motivated this change: Boss runs the "claude" host
    // harness, which has had a HostHarnessAdapter since 2026-07-25. Under the
    // old `harness === "hermes"` rule the modal treated it as a normal
    // container switch and offered the (inapplicable) cli-bridge harness
    // selector. Keyed off runtime_switchable, any adapter-backed host harness
    // gets the in-place path.
    it.each(["claude", "grok", "kimi"])(
      "treats a switchable host agent on harness '%s' as in-place, not just hermes",
      async (harness) => {
        vi.spyOn(api.agents, "previewRuntimeSwitch").mockResolvedValue(mkPreview());
        const agent = mkAgent({
          agent_runtime: "host",
          harness: harness as Agent["harness"],
          runtime_switchable: true,
          runtime_switch_blocked_reason: null,
        });
        renderWithQuery(
          <RuntimeSwitchModal
            open
            onClose={() => {}}
            agent={agent}
            targetRuntimeId="rt-new"
            onConfirm={async () => null}
          />,
        );
        await waitFor(() => expect(screen.getByText("Qwen 3.6")).toBeInTheDocument());
        expect(screen.getByTestId("host-inplace-hint")).toBeInTheDocument();
        // Host harnesses live outside the cli-bridge compat matrix, so the
        // harness selector must stay hidden for them.
        expect(screen.queryByLabelText("Harness")).not.toBeInTheDocument();
        expect(screen.getByRole("button", { name: /switch/i })).toBeEnabled();
      },
    );

    // Positive control for the two assertions above — without it, a renamed
    // label or hint would make them pass vacuously.
    it("shows the harness selector and no in-place hint for a cli-bridge agent", async () => {
      vi.spyOn(api.agents, "previewRuntimeSwitch").mockResolvedValue(mkPreview());
      renderWithQuery(
        <RuntimeSwitchModal
          open
          onClose={() => {}}
          agent={mkAgent()}
          targetRuntimeId="rt-new"
          onConfirm={async () => null}
        />,
      );
      await waitFor(() => expect(screen.getByText("Qwen 3.6")).toBeInTheDocument());
      expect(screen.getByLabelText("Harness")).toBeInTheDocument();
      expect(screen.queryByTestId("host-inplace-hint")).not.toBeInTheDocument();
    });

    it("does not take the in-place path for a host agent the backend blocked", async () => {
      vi.spyOn(api.agents, "previewRuntimeSwitch").mockResolvedValue(mkPreview());
      const agent = mkAgent({
        agent_runtime: "host",
        harness: "openclaw" as Agent["harness"],
        runtime_switchable: false,
        runtime_switch_blocked_reason:
          "Host agent with harness 'openclaw' has no host adapter.",
      });
      renderWithQuery(
        <RuntimeSwitchModal
          open
          onClose={() => {}}
          agent={agent}
          targetRuntimeId="rt-new"
          onConfirm={async () => null}
        />,
      );
      await waitFor(() => expect(screen.getByText("Qwen 3.6")).toBeInTheDocument());
      expect(screen.queryByTestId("host-inplace-hint")).not.toBeInTheDocument();
    });

    it("still hard-blocks a plain cli-bridge single_instance switch (regression)", async () => {
      vi.spyOn(api.agents, "previewRuntimeSwitch").mockResolvedValue(
        mkPreview({
          old_runtime: {
            id: "rt-hermes",
            slug: "hermes-host",
            display_name: "Hermes",
            runtime_type: "hermes",
            model_identifier: null,
            single_instance: true,
          },
        }),
      );
      renderWithQuery(
        <RuntimeSwitchModal
          open
          onClose={() => {}}
          agent={mkAgent()} // cli-bridge, no harness
          targetRuntimeId="rt-new"
          onConfirm={async () => null}
        />,
      );
      await waitFor(() =>
        expect(screen.getByTestId("single-instance-lock-banner")).toBeInTheDocument(),
      );
      expect(screen.getByRole("button", { name: /switch/i })).toBeDisabled();
    });
  });

  describe("harness selector (ADR-056)", () => {
    // mkAgent() has no `harness` set → the select also renders the explicit
    // "Default (derived from provider)" placeholder option (review finding 2),
    // on top of the 3 harnesses from the compat matrix.
    it("renders all 3 harness options from the compat matrix plus the standard placeholder", async () => {
      vi.spyOn(api.agents, "previewRuntimeSwitch").mockResolvedValue(
        mkPreview({ new_runtime: { ...mkPreview().new_runtime, slug: "qwen-general" } }),
      );
      vi.spyOn(api.runtimes, "compatMatrix").mockResolvedValue(mkCompatMatrix());
      renderWithQuery(
        <RuntimeSwitchModal
          open
          onClose={() => {}}
          agent={mkAgent()}
          targetRuntimeId="rt-new"
          onConfirm={async () => null}
        />,
      );
      const select = await screen.findByLabelText(/harness/i);
      await waitFor(() => expect(within(select).getAllByRole("option")).toHaveLength(4));
    });

    it("disables the option incompatible with the target runtime and shows the reason as a title", async () => {
      vi.spyOn(api.agents, "previewRuntimeSwitch").mockResolvedValue(
        mkPreview({ new_runtime: { ...mkPreview().new_runtime, slug: "qwen-general" } }),
      );
      vi.spyOn(api.runtimes, "compatMatrix").mockResolvedValue(mkCompatMatrix());
      renderWithQuery(
        <RuntimeSwitchModal
          open
          onClose={() => {}}
          agent={mkAgent()}
          targetRuntimeId="rt-new"
          onConfirm={async () => null}
        />,
      );
      const select = await screen.findByLabelText(/harness/i);
      await waitFor(() => expect(within(select).getAllByRole("option")).toHaveLength(4));
      const claudeOption = within(select).getByRole("option", { name: /claude code.*incompatible/i });
      expect(claudeOption).toBeDisabled();
      expect(claudeOption).toHaveAttribute(
        "title",
        "Claude Code unterstützt nur Anthropic-Protokoll-Runtimes",
      );
    });

    it("submit passes the selected harness through onConfirm", async () => {
      vi.spyOn(api.agents, "previewRuntimeSwitch").mockResolvedValue(
        mkPreview({ new_runtime: { ...mkPreview().new_runtime, slug: "qwen-general" } }),
      );
      vi.spyOn(api.runtimes, "compatMatrix").mockResolvedValue(mkCompatMatrix());
      const onConfirm = vi.fn().mockResolvedValue(mkPreview());
      renderWithQuery(
        <RuntimeSwitchModal
          open
          onClose={() => {}}
          agent={mkAgent()}
          targetRuntimeId="rt-new"
          onConfirm={onConfirm}
        />,
      );
      const select = await screen.findByLabelText(/harness/i);
      await waitFor(() => expect(within(select).getAllByRole("option")).toHaveLength(4));
      await userEvent.selectOptions(select, "omp");
      await userEvent.click(screen.getByRole("button", { name: /switch/i }));
      await waitFor(() =>
        expect(onConfirm).toHaveBeenCalledWith({ force_when_in_progress: false, harness: "omp" }),
      );
    });

    it("blocks submission when the preselected harness is incompatible with the target runtime and shows the reason", async () => {
      vi.spyOn(api.agents, "previewRuntimeSwitch").mockResolvedValue(
        mkPreview({ new_runtime: { ...mkPreview().new_runtime, slug: "qwen-general" } }),
      );
      vi.spyOn(api.runtimes, "compatMatrix").mockResolvedValue(mkCompatMatrix());
      const onConfirm = vi.fn().mockResolvedValue(mkPreview());
      renderWithQuery(
        <RuntimeSwitchModal
          open
          onClose={() => {}}
          agent={mkAgent({ harness: "claude" })}
          targetRuntimeId="rt-new"
          onConfirm={onConfirm}
        />,
      );
      const select = await screen.findByLabelText(/harness/i);
      await waitFor(() => expect(within(select).getAllByRole("option")).toHaveLength(3));
      expect(select).toHaveValue("claude");

      const submit = screen.getByRole("button", { name: /switch/i });
      await waitFor(() => expect(submit).toBeDisabled());
      expect(
        screen.getByText("Claude Code unterstützt nur Anthropic-Protokoll-Runtimes"),
      ).toBeInTheDocument();

      await userEvent.click(submit);
      expect(onConfirm).not.toHaveBeenCalled();
    });

    it("shows the explicit standard placeholder option, selected, when agent.harness is null", async () => {
      vi.spyOn(api.agents, "previewRuntimeSwitch").mockResolvedValue(
        mkPreview({ new_runtime: { ...mkPreview().new_runtime, slug: "qwen-general" } }),
      );
      vi.spyOn(api.runtimes, "compatMatrix").mockResolvedValue(mkCompatMatrix());
      renderWithQuery(
        <RuntimeSwitchModal
          open
          onClose={() => {}}
          agent={mkAgent()}
          targetRuntimeId="rt-new"
          onConfirm={async () => null}
        />,
      );
      const select = await screen.findByLabelText(/harness/i);
      await waitFor(() =>
        expect(
          within(select).getByRole("option", { name: "Default (derived from provider)" }),
        ).toBeInTheDocument(),
      );
      expect(select).toHaveValue("");
    });
  });
});
